"""带文本框的 OCR 封装。

和老 scan.py 的区别：这里要**保留检测框坐标**，因为下游靠几何位置把文本框分配给座位。
scan._flatten() 只吐 (text, score) 二元组，对它我们一个字都不改 —— 它在老 ROI 路径上
还有用，改它会波及那边的行为。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import scan  # noqa: E402  (冻结的标定工具，这里只复用它的 preprocess / RapidEngine)


@dataclass
class OcrBox:
    """一个检测到的文本框。x1,y1,x2,y2 是原图像素坐标（轴对齐外接矩形）。"""

    text: str
    score: float
    x1: float
    y1: float
    x2: float
    y2: float
    polygon: list[tuple[float, float]]

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2

    @property
    def center(self) -> tuple[float, float]:
        return self.cx, self.cy

    @property
    def w(self) -> float:
        return self.x2 - self.x1

    @property
    def h(self) -> float:
        return self.y2 - self.y1

    def normalized_center(self, size: tuple[int, int]) -> tuple[float, float]:
        w, h = size
        return self.cx / w, self.cy / h

    def to_debug(self) -> dict:
        return {
            "text": self.text,
            "score": round(self.score, 4),
            "box": [round(self.x1, 1), round(self.y1, 1), round(self.x2, 1), round(self.y2, 1)],
        }


def _parse_boxes(obj) -> list[OcrBox]:
    """从 RapidOCR 的 det+rec 返回里挖出带框的结果。

    RapidOCR 的形状是 [[box, text, score], ...]，box 是 4 个 [x,y] 点。
    这里只认这一种结构 —— 结构对不上就少吐结果，不猜。
    """
    out: list[OcrBox] = []
    if not isinstance(obj, (list, tuple)):
        return out

    for item in obj:
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            continue
        box, text, score = item
        if not isinstance(text, str):
            continue
        try:
            pts = [(float(p[0]), float(p[1])) for p in box]
        except (TypeError, ValueError, IndexError):
            continue
        if not pts:
            continue
        try:
            sc = float(score)
        except (TypeError, ValueError):
            sc = 0.0
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        out.append(OcrBox(text=text, score=sc,
                          x1=min(xs), y1=min(ys), x2=max(xs), y2=max(ys),
                          polygon=pts))
    return out


class OcrEngine:
    """RapidOCR 的 det+rec 封装，外加一个显式可控的检测缩放。

    det_side_len 不为空时，我们自己把图缩到这个长边再送检，检测框再换算回原图坐标。
    显式做这一步是为了让结果可预期 —— 引擎内部的默认缩放策略各版本不一致，
    「1440 宽能读对」不代表「1080 宽还能读对」。
    """

    def __init__(self, det_side_len: int | None = None) -> None:
        self.det_side_len = det_side_len
        self._rapid = scan.RapidEngine()

    def read_boxes(self, img: np.ndarray) -> list[OcrBox]:
        h, w = img.shape[:2]
        feed = img
        scale = 1.0
        if self.det_side_len:
            long_side = max(h, w)
            if long_side != self.det_side_len:
                scale = self.det_side_len / long_side
                feed = _resize_long_side(img, self.det_side_len)

        result, _ = self._rapid._engine(feed)   # det + cls + rec 全开
        boxes = _parse_boxes(result)

        if scale != 1.0:
            inv = 1.0 / scale
            for b in boxes:
                b.x1, b.y1, b.x2, b.y2 = b.x1 * inv, b.y1 * inv, b.x2 * inv, b.y2 * inv
                b.polygon = [(x * inv, y * inv) for x, y in b.polygon]
        return boxes

    def read_text(self, img: np.ndarray) -> list[tuple[str, float]]:
        """只做识别不做检测 —— 名字重试阶梯用这个（槽位已经框好了）。"""
        return self._rapid.read(img)


def _resize_long_side(img: np.ndarray, target: int) -> np.ndarray:
    import cv2

    h, w = img.shape[:2]
    long_side = max(h, w)
    if long_side == target:
        return img
    f = target / long_side
    interp = cv2.INTER_AREA if f < 1 else cv2.INTER_CUBIC
    return cv2.resize(img, (max(1, int(round(w * f))), max(1, int(round(h * f)))),
                      interpolation=interp)


def preprocess_variant(roi: np.ndarray, mode: str, upscale: float) -> np.ndarray:
    """复用 scan.preprocess —— 它已经处理好 Otsu 的黑白方向问题了。"""
    return scan.preprocess(roi, mode, upscale)
