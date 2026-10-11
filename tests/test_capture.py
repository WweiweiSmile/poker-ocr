"""capture 的单测。

重点是把**抽帧去重的循环**在「不真的截屏、不真的等待」下跑遍 ——
这类循环一旦只能靠开着模拟器手点来验，就等于没验。所以 capture_fn /
sleep_fn / clock 都从外面注入。
"""

from __future__ import annotations

import argparse

import numpy as np
import pytest

from poker_ocr import capture


def _img(stripe: int = -1) -> np.ndarray:
    """造一张有结构的假帧：横向渐变 + 可选一条竖亮条（位置由 stripe 给）。

    变化的载体必须是**结构**（亮条在哪），不能是「一块纯色的值」：
    dhash 比的是相邻像素，纯色块内部怎么改色都不翻位 ——
    这条限制见 test_纯色区域不产生信息。
    """
    g = np.tile((np.arange(32) * 8).astype(np.uint8), (24, 1))
    img = np.dstack([g, g, g])
    if stripe >= 0:
        img[:, stripe:stripe + 3] = 255
    return img


def test_dhash_相同图相同_hash():
    a, b = _img(), _img()
    assert capture.dhash(a) == capture.dhash(b)
    assert capture.hamming(capture.dhash(a), capture.dhash(b)) == 0


def test_dhash_结构不同则_hash_不同():
    a, b = _img(), _img(stripe=12)
    assert capture.hamming(capture.dhash(a), capture.dhash(b)) > 0


def test_纯色区域不产生信息():
    """把 dhash 的限制钉死：纯色图恒为 0，改色也不翻位。

    推论：**去重阈值只能保证「画面有明显结构变化」时出帧**。
    如果模拟器靠「一小块纯色」表达状态（比如一个小圆点变色），
    这里的全局 dhash 会漏 —— 那时得改成按 ROI 哈希。别到线上才发现。
    """
    assert capture.dhash(np.zeros((24, 24, 3), np.uint8)) == 0
    assert capture.dhash(np.full((24, 24, 3), 200, np.uint8)) == 0


def _frames(images, *, interval=1.0, min_change=1, duration=6.0, max_frames=None):
    """用假时钟 + 假截屏把 iter_frames 跑完，返回输出的帧列表。"""
    clock = [0.0]
    seq = list(images)

    def capture_fn(_rect):
        return seq.pop(0) if seq else images[-1]

    def sleep_fn(d):
        clock[0] += d

    return list(capture.iter_frames(
        interval=interval, min_change=min_change, duration=duration,
        max_frames=max_frames, capture_fn=capture_fn, sleep_fn=sleep_fn,
        clock=lambda: clock[0]))


def test_静止画面只出一帧():
    frames = _frames([_img()] * 6)
    assert len(frames) == 1
    assert frames[0].index == 0 and frames[0].delta == -1


def test_变化才出帧_并且记录汉明距离():
    a, b, c = _img(), _img(stripe=4), _img(stripe=20)
    frames = _frames([a, a, b, b, b, c])
    assert [f.index for f in frames] == [0, 1, 2]          # 三组各出一张
    assert frames[0].delta == -1                            # 第一张没有前驱
    assert frames[1].delta >= 1 and frames[2].delta >= 1


def test_阈值卡住小变化():
    a, b = _img(), _img(stripe=16)
    d = capture.hamming(capture.dhash(a), capture.dhash(b))
    assert d >= 1
    assert len(_frames([a, b], min_change=d + 1)) == 1      # 阈值大于差异 -> 当重复
    assert len(_frames([a, b], min_change=d)) == 2          # 阈值等于差异 -> 采纳


def test_duration_到点就停():
    # 两张交替：每次采样都相对上一输出帧有变化，于是「出帧数 == 采样次数」
    frames = _frames([_img(stripe=0), _img(stripe=16)] * 10, interval=1.0, duration=3.0)
    assert len(frames) == 3


def test_max_frames_到数就停():
    frames = _frames([_img(stripe=0), _img(stripe=16)] * 10, max_frames=2)
    assert len(frames) == 2


def test_第一帧必定输出_哪怕后面全一样():
    frames = _frames([_img(stripe=8)] * 5, max_frames=1)
    assert len(frames) == 1 and frames[0].delta == -1


@pytest.mark.parametrize("text, expect", [
    (None, None),
    ("100,80,1200,900", (100, 80, 1200, 900)),
    ("0,0,10,20", (0, 0, 10, 20)),
])
def test_区域解析(text, expect):
    assert capture._parse_region(text) == expect


def test_区域解析_格式错要报错():
    with pytest.raises(argparse.ArgumentTypeError):
        capture._parse_region("1,2,3")
