#!/usr/bin/env python3
"""
牌桌截图字段提取 —— 第 0 步验证脚本

目的：在训练任何模型之前，先验证「固定版式 + ROI 裁剪 + OCR」到底能不能读对。
      这一步能跑通，整个项目就不用碰 YOLO。

用法：
    # 1. 量坐标：交互框选每个字段，存进 rois.json
    python scan.py calibrate shot.png --fields pot,hero_stack,villain_name:text

    # 2. 跑识别：按 rois.json 裁剪 + OCR，输出结构化 JSON
    python scan.py run shot.png

    # 3. 只想看看坐标画在哪，不跑 OCR
    python scan.py run shot.png --save-annotated

字段类型（--fields 里用冒号指定，默认 number）：
    pot:number       筹码数/底池/下注额 —— 会做数字清洗，输出 value 为 int/float
    villain_name:text  玩家名等文本 —— 原样输出

引擎：默认 rapidocr（装起来轻，14MB）。读不准再换 --engine paddleocr。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
DEFAULT_ROI_FILE = HERE / "rois.json"

# ─────────────────────────── 可调参数 ───────────────────────────

DEFAULT_BASE_SIZE = (1920, 1080)   # 归一化基准尺寸。把这里改成你截图的原生分辨率
DEFAULT_UPSCALE = 3.0              # 裁剪图放大倍数（小字号数字 + 放大 数据提升明显）
MAX_CALIB_WIDTH = 1280             # 框选窗口最大宽度（防止超出屏幕，坐标会自动换算回去）


# ─────────────────────────── 图像处理 ───────────────────────────

def load_image(path: Path) -> np.ndarray:
    img = cv2.imread(str(path))
    if img is None:
        sys.exit(f"读不了这张图：{path}")
    return img


def normalize(img: np.ndarray, base_size: tuple[int, int]) -> np.ndarray:
    """把截图统一缩放到基准尺寸 —— 解决「分辨率不一致导致坐标失效」"""
    if (img.shape[1], img.shape[0]) == base_size:
        return img
    return cv2.resize(img, base_size, interpolation=cv2.INTER_AREA)


def preprocess(roi: np.ndarray, mode: str, upscale: float) -> np.ndarray:
    """裁剪后的预处理。始终返回 3 通道 BGR，避免各家 OCR 对灰度图的兼容问题。"""
    if upscale != 1.0:
        roi = cv2.resize(roi, None, fx=upscale, fy=upscale,
                         interpolation=cv2.INTER_CUBIC)

    if mode == "raw":
        return roi

    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

    if mode == "gray":
        return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)

    if mode == "otsu":
        _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        # Otsu 不保证文字是黑的：看边框像素，若偏暗说明背景是深色，需要反转
        border = np.concatenate([
            bw[0, :], bw[-1, :], bw[:, 0], bw[:, -1],
        ])
        if border.mean() < 127:
            bw = cv2.bitwise_not(bw)
        return cv2.cvtColor(bw, cv2.COLOR_GRAY2BGR)

    sys.exit(f"未知的 --preprocess 模式：{mode}")


# ─────────────────────────── OCR 引擎 ───────────────────────────

def _flatten(obj, out: list[tuple[str, float]]) -> None:
    """从各家 OCR 五花八门的返回结构里挖出 (text, score)。

    这里的写法刻意防御性很强：不同引擎/版本的返回格式差别很大，
    结构对不上时宁可少挖出字段，也不要直接崩掉。用 --debug 看原始返回。
    """
    if obj is None:
        return

    # RapidOCR: [[box, text, score], ...]  或（关掉 det 时）[[text, score], ...]
    if isinstance(obj, (list, tuple)):
        if len(obj) == 3 and isinstance(obj[1], str):
            try:
                out.append((obj[1], float(obj[2])))
            except (TypeError, ValueError):
                out.append((obj[1], 0.0))
            return
        if len(obj) == 2 and isinstance(obj[0], str):
            try:
                out.append((obj[0], float(obj[1])))
            except (TypeError, ValueError):
                out.append((obj[0], 0.0))
            return
        for item in obj:
            _flatten(item, out)
        return

    if isinstance(obj, dict):
        # PaddleOCR 3.x 流水线风格：{'rec_texts': [...], 'rec_scores': [...]}
        for key in ("rec_texts", "texts"):
            if key in obj and isinstance(obj[key], (list, tuple)):
                scores = obj.get("rec_scores") or obj.get("scores") or []
                for i, t in enumerate(obj[key]):
                    s = float(scores[i]) if i < len(scores) else 0.0
                    out.append((str(t), s))
                return
        # 单条风格：{'rec_text': '...', 'rec_score': 0.9}
        for key in ("rec_text", "text"):
            if isinstance(obj.get(key), str):
                raw_score = obj.get("rec_score", obj.get("score", 0.0))
                try:
                    score = float(raw_score)
                except (TypeError, ValueError):
                    score = 0.0
                out.append((obj[key], score))
                return
        for v in obj.values():
            _flatten(v, out)
        return

    # PaddleOCR 3.x 的 result 对象：常见 .json / .to_dict() / .res
    for attr in ("json", "to_dict", "res"):
        v = getattr(obj, attr, None)
        if v is None:
            continue
        _flatten(v() if callable(v) else v, out)
        return

    for attr in ("rec_text", "text"):
        v = getattr(obj, attr, None)
        if isinstance(v, str):
            try:
                score = float(getattr(obj, "rec_score", 0.0) or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            out.append((v, score))
            return


class RapidEngine:
    """RapidOCR —— ONNX Runtime，纯离线，包体积小，装起来最省事"""
    name = "rapidocr"

    def __init__(self) -> None:
        from rapidocr_onnxruntime import RapidOCR
        self._engine = RapidOCR()

    def read(self, img: np.ndarray) -> list[tuple[str, float]]:
        # 关掉检测和方向分类：我们已知框在哪，只跑识别
        result, _ = self._engine(img, use_det=False, use_cls=False, use_rec=True)
        out: list[tuple[str, float]] = []
        _flatten(result, out)
        return out

    def raw(self, img: np.ndarray):
        return self._engine(img, use_det=False, use_cls=False, use_rec=True)


class PaddleEngine:
    """PaddleOCR 3.x 单模块识别 —— 精度通常更高，装起来更重"""
    name = "paddleocr"

    def __init__(self) -> None:
        from paddleocr import TextRecognition
        self._engine = TextRecognition()

    def read(self, img: np.ndarray) -> list[tuple[str, float]]:
        out: list[tuple[str, float]] = []
        _flatten(self._engine.predict(img), out)
        return out

    def raw(self, img: np.ndarray):
        return self._engine.predict(img)


ENGINES = {"rapidocr": RapidEngine, "paddleocr": PaddleEngine}


# ─────────────────────────── 后处理 ───────────────────────────

# 只在 number 字段上做的易混字符纠正（注意：必须先摘掉单位后缀，否则 B→8 会吃掉 BB）
_NUM_TRANSLATE = str.maketrans({
    "O": "0", "o": "0", "D": "0", "Q": "0",
    "l": "1", "I": "1", "|": "1", "i": "1",
    "B": "8", "S": "5", "Z": "2", "z": "2", "g": "9", "q": "9",
    "，": ",", " ": "", "_": "", "'": "",
})

# 只认这几种无歧义的单位后缀。
# 特意不收单字母 B —— 它和数字 8 的误识无法区分，宁可报错也不要猜。
_UNIT_RE = re.compile(r"(BB|[KkMm])\s*$")

# 会被纠正的字符集合。命中说明这个值是「猜」出来的，下游该降低信任。
_CORRECTED_CHARS = frozenset("OoDQlI|iBSZzgq")


def clean_number(text: str) -> tuple[int | float | None, str | None, bool]:
    """把 OCR 出来的字符串收拾成一个数。

    返回 (数值, 单位, 是否发生过纠正)。
    单位指 K/M/BB 这类后缀 —— 具体怎么换算（1K 等于多少筹码）属于业务规则，
    脚本不替你决定，只把后缀原样报出来。

    corrected=True 意味着原始文本里有字母被当成数字纠正过（比如 O→0、B→8）。
    这种纠正绝大多数时候是对的，但例如 '12B' 会被读成 128 ——
    如果那个 B 其实是大盲单位，这个值就是错的。所以下游拿到 corrected=True 时
    应该考虑人工复核，而不是直接采信。
    """
    t = text.strip()

    # 第一步：先摘单位后缀，避免被下面的 B→8 纠正误伤
    unit = None
    m = _UNIT_RE.search(t)
    if m:
        unit = m.group(1).upper()
        t = t[: m.start()]

    # 第二步：纠正常见误识 + 去掉千分位
    corrected = any(ch in _CORRECTED_CHARS for ch in t)
    t = t.translate(_NUM_TRANSLATE).replace(",", "").strip()

    # 第三步：剩下的必须是纯数字，有残留字母就老实报失败
    if not re.fullmatch(r"\d+(?:\.\d+)?", t):
        return None, unit, corrected

    value: int | float = float(t) if "." in t else int(t)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return value, unit, corrected


# ─────────────────────────── 配置读写 ───────────────────────────

def parse_fields(spec: str) -> list[dict]:
    """把 'pot,hero_stack,villain_name:text' 解析成字段定义列表"""
    fields = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" in chunk:
            name, ftype = chunk.rsplit(":", 1)
        else:
            name, ftype = chunk, "number"
        ftype = ftype.strip().lower()
        if ftype not in ("number", "text"):
            sys.exit(f"字段 {name} 的类型只能是 number 或 text，收到：{ftype}")
        fields.append({"name": name.strip(), "type": ftype})
    return fields


def load_rois(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"找不到 {path}\n先跑一次 calibrate 量坐标：\n"
                 f"  python scan.py calibrate 你的截图.png --fields pot,hero_stack")
    return json.loads(path.read_text(encoding="utf-8"))


# ─────────────────────────── calibrate ───────────────────────────

def fit_for_display(img: np.ndarray, max_w: int) -> tuple[np.ndarray, float]:
    """窗口可能超出屏幕，等比缩小显示，坐标再换算回去"""
    h, w = img.shape[:2]
    if w <= max_w:
        return img, 1.0
    scale = max_w / w
    return cv2.resize(img, (int(w * scale), int(h * scale)),
                      interpolation=cv2.INTER_AREA), scale


def cmd_calibrate(args) -> None:
    img = normalize(load_image(args.image), args.base_size)
    fields = parse_fields(args.fields)
    if not fields:
        sys.exit("--fields 是空的，没东西可量")

    view, scale = fit_for_display(img, MAX_CALIB_WIDTH)
    print(f"原图 {img.shape[1]}x{img.shape[0]}，显示缩放 {scale:.3f}")
    print("每个字段：拖框选区域 → 回车确认；想跳过就按 c 或 ESC\n")

    existing = {}
    if args.roi_file.exists():
        try:
            existing = {f["name"]: f for f in json.loads(
                args.roi_file.read_text(encoding="utf-8")).get("fields", [])}
        except (json.JSONDecodeError, KeyError):
            pass

    results = []
    for field in fields:
        name = field["name"]
        print(f"→ 框选 {name}（{field['type']}）...")
        x, y, w, h = cv2.selectROI(f"calibrate: {name}", view,
                                   showCrosshair=True, fromCenter=False)
        cv2.destroyAllWindows()
        if w == 0 or h == 0:
            if name in existing:
                print(f"  跳过，沿用已有坐标 {existing[name]['roi']}")
                results.append(existing[name])
            else:
                print("  跳过（没有旧坐标，该字段将缺失）")
            continue
        # 换算回原图坐标
        roi = [int(round(x / scale)), int(round(y / scale)),
               int(round((x + w) / scale)), int(round((y + h) / scale))]
        print(f"  ROI = {roi}")
        results.append({"name": name, "type": field["type"], "roi": roi})

    payload = {"base_size": list(args.base_size), "fields": results}
    args.roi_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                             encoding="utf-8")
    print(f"\n已写入 {args.roi_file}（{len(results)} 个字段）")


# ─────────────────────────── run ───────────────────────────

def cmd_run(args) -> None:
    cfg = load_rois(args.roi_file)
    base_size = tuple(cfg.get("base_size", DEFAULT_BASE_SIZE))
    fields = cfg.get("fields", [])
    if not fields:
        sys.exit(f"{args.roi_file} 里没有字段，重新 calibrate 一次吧")

    img = normalize(load_image(args.image), base_size)

    engine = None
    if not args.save_annotated_only:
        engine = ENGINES[args.engine]()

    annotated = img.copy()
    out_fields: dict[str, dict] = {}
    started = time.perf_counter()

    for field in fields:
        name, ftype = field["name"], field["type"]
        x1, y1, x2, y2 = field["roi"]
        h, w = img.shape[:2]
        x1, y1 = max(0, min(x1, w)), max(0, min(y1, h))
        x2, y2 = max(x1 + 1, min(x2, w)), max(y1 + 1, min(y2, h))

        roi = img[y1:y2, x1:x2]
        cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.putText(annotated, name, (x1, max(14, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv2.LINE_AA)

        if args.dump_crops:
            crop_dir = Path(args.dump_crops)
            crop_dir.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(crop_dir / f"{name}.png"), roi)

        if engine is None:
            continue

        prepared = preprocess(roi, args.preprocess, args.upscale)

        if args.debug:
            print(f"\n[debug] {name} 原始返回：\n{engine.raw(prepared)!r}")

        try:
            hits = engine.read(prepared)
        except Exception as exc:  # 单个字段失败不该带崩整轮
            out_fields[name] = {"raw": None, "value": None, "score": 0.0,
                                "ok": False, "error": f"{type(exc).__name__}: {exc}"}
            continue

        if not hits:
            out_fields[name] = {"raw": None, "value": None, "score": 0.0,
                                "ok": False, "error": "没识别出文字"}
            continue

        raw_text = " ".join(t for t, _ in hits).strip()
        score = min(s for _, s in hits)

        if ftype == "number":
            value, unit, corrected = clean_number(raw_text)
            entry = {"raw": raw_text, "value": value, "score": round(score, 4),
                     "ok": value is not None}
            if unit:
                entry["unit"] = unit
            if corrected:
                entry["corrected"] = True
            if value is None:
                entry["error"] = "清洗后不是数字"
            out_fields[name] = entry
        else:
            out_fields[name] = {"raw": raw_text, "value": raw_text,
                                "score": round(score, 4), "ok": True}

    elapsed_ms = (time.perf_counter() - started) * 1000

    if args.save_annotated:
        out_path = Path(args.save_annotated)
        cv2.imwrite(str(out_path), annotated)
        print(f"标注图已存到 {out_path}")

    payload = {
        "image": str(args.image),
        "base_size": list(base_size),
        "engine": args.engine,
        "preprocess": args.preprocess,
        "fields": out_fields,
        "elapsed_ms": round(elapsed_ms, 1),
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))

    if engine is not None:
        bad = [n for n, v in out_fields.items() if not v["ok"]]
        if bad:
            print(f"\n⚠️  读失败的字段：{', '.join(bad)}", file=sys.stderr)
            print("可以试试：--preprocess gray / --preprocess otsu / --upscale 5",
                  file=sys.stderr)

        # 读出来了但可能不对的：纠正过、或置信度偏低。第 0 步重点就看这份名单。
        suspect = [n for n, v in out_fields.items()
                   if v["ok"] and (v.get("corrected") or v["score"] < args.min_score)]
        if suspect:
            print(f"\n⚠️  需要复核的字段（纠正过或置信度<{args.min_score}）："
                  f"{', '.join(suspect)}", file=sys.stderr)


# ─────────────────────────── CLI ───────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="牌桌截图字段提取（第 0 步验证）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("image", type=Path, help="截图路径")
        p.add_argument("--roi-file", type=Path, default=DEFAULT_ROI_FILE,
                       help=f"ROI 配置文件（默认 {DEFAULT_ROI_FILE.name}）")
        p.add_argument("--base-size", default=f"{DEFAULT_BASE_SIZE[0]}x{DEFAULT_BASE_SIZE[1]}",
                       help="归一化基准尺寸，格式 1920x1080")

    cal = sub.add_parser("calibrate", help="交互框选，量出每个字段的 ROI 坐标")
    common(cal)
    cal.add_argument("--fields", required=True,
                     help="逗号分隔，如 pot,hero_stack,villain_name:text")
    cal.set_defaults(func=cmd_calibrate)

    run = sub.add_parser("run", help="按 rois.json 裁剪 + OCR")
    common(run)
    run.add_argument("--engine", choices=sorted(ENGINES), default="rapidocr")
    run.add_argument("--upscale", type=float, default=DEFAULT_UPSCALE,
                     help=f"裁剪图放大倍数（默认 {DEFAULT_UPSCALE}）")
    run.add_argument("--min-score", type=float, default=0.9,
                     help="低于这个置信度就列入「需要复核」名单（默认 0.9）")
    run.add_argument("--preprocess", choices=("raw", "gray", "otsu"), default="raw",
                     help="raw=不处理（先试这个）, gray=灰度, otsu=二值化")
    run.add_argument("--dump-crops", type=Path, metavar="DIR",
                     help="把每个字段的裁剪图存下来（以后攒牌面训练集也用得上）")
    run.add_argument("--save-annotated", nargs="?", const="annotated.png", metavar="PATH",
                     help="把 ROI 框画在图上存下来，用于核对坐标")
    run.add_argument("--save-annotated-only", action="store_true",
                     help="只画框，不跑 OCR")
    run.add_argument("--debug", action="store_true", help="打印 OCR 原始返回")
    run.set_defaults(func=cmd_run)

    args = parser.parse_args()

    if hasattr(args, "base_size") and isinstance(args.base_size, str):
        try:
            w, h = args.base_size.lower().split("x")
            args.base_size = (int(w), int(h))
        except ValueError:
            sys.exit(f"--base-size 格式不对：{args.base_size}，应该是 1920x1080 这样")

    args.func(args)


if __name__ == "__main__":
    main()
