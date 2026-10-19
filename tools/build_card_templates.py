#!/usr/bin/env python3
"""从标注样本生成点数模板图集 poker_ocr/assets/card_rank_templates.png。

为什么需要生成而不是手画：牌面用的是 WPK 的图形化字体，"10" 还是双字宽，
自己画字形反而对不上。这里直接从真机截图里裁出每个点数的一张代表样本，
量化后拼成 13 列图集；cards.py 运行时只读这张图，不再依赖样本图片。

模板的**跨花色**能力来自「点数形与花色无关」：K♠ 的 K 和 K♥ 的 K 是同一个字形，
所以每个点数一张就够（实测 16 张样本、81 张牌零错配）。

    .venv/bin/python tools/build_card_templates.py

样本换了（App 改版、字体变了）时重跑一次即可。
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr import cards  # noqa: E402
from poker_ocr.profile import Profile  # noqa: E402

PROFILE_PATH = ROOT / "config" / "table_wpk_1080x2400.json"
IMAGE_DIR = ROOT / "images" / "card"
OUT_PATH = ROOT / "poker_ocr" / "assets" / "card_rank_templates.png"

# rank -> (图片文件名, 区域, 该区域里第几张牌)
# 故意让每个点数来自不同的花色/不同帧，避免模板过拟合某一种渲染。
SOURCES: dict[str, tuple[str, str, int]] = {
    "2":  ("微信图片_20261019225352_142_351.jpg", "board", 1),       # 2♥
    "3":  ("微信图片_20261019225348_138_351.jpg", "board", 0),       # 3♦
    "4":  ("微信图片_20261019225350_140_351.jpg", "board", 2),       # 4♠
    "5":  ("微信图片_20261019225348_138_351.jpg", "hero_cards", 0),  # 5♥
    "6":  ("微信图片_20261019225350_140_351.jpg", "board", 1),       # 6♥
    "7":  ("微信图片_20261019225403_152_351.jpg", "board", 0),       # 7♦
    "8":  ("微信图片_20261019225350_140_351.jpg", "hero_cards", 1),  # 8♠
    "9":  ("微信图片_20261019225351_141_351.jpg", "board", 1),       # 9♠
    "10": ("微信图片_20261019225350_140_351.jpg", "board", 3),       # 10♦
    "J":  ("微信图片_20261019225352_142_351.jpg", "board", 2),       # J♣
    "Q":  ("微信图片_20261019225348_138_351.jpg", "board", 1),       # Q♣
    "K":  ("微信图片_20261019225348_138_351.jpg", "hero_cards", 1),  # K♠
    "A":  ("微信图片_20261019225348_138_351.jpg", "board", 2),       # A♦
}


def glyph_from_source(profile: Profile, rank: str) -> np.ndarray:
    filename, slot_name, index = SOURCES[rank]
    path = IMAGE_DIR / filename
    img = cv2.imread(str(path))
    if img is None:
        raise SystemExit(f"读不了样本图：{path}")

    size = (img.shape[1], img.shape[0])
    rect = profile.slot_to_px(profile.global_slots[slot_name], size)
    x1, y1, x2, y2 = rect
    roi = img[y1:y2, x1:x2]

    rects = cards._detect_card_rects(roi)
    if index >= len(rects):
        raise SystemExit(
            f"{filename} 的 {slot_name} 只检出 {len(rects)} 张牌，取不到第 {index} 张")
    card = cards._canonical_card(roi, rects[index])
    suit, _, _ = cards._classify_suit(card)
    glyph = cards._rank_glyph(card, suit)
    if glyph is None:
        raise SystemExit(f"{filename} 的 {rank} 没裁出点数字形")
    return glyph


def main() -> int:
    profile = Profile.load(PROFILE_PATH)
    for key in ("board", "hero_cards"):
        if key not in profile.global_slots:
            raise SystemExit(f"{PROFILE_PATH} 缺少 global_slots.{key}")

    cells = [glyph_from_source(profile, rank) for rank in cards.RANKS]
    atlas = np.hstack(cells)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(OUT_PATH), atlas):
        raise SystemExit(f"写图集失败：{OUT_PATH}")

    print(f"✅ 已生成 {OUT_PATH.relative_to(ROOT)}  {atlas.shape[1]}x{atlas.shape[0]}"
          f"（{len(cells)} 个点数：{' '.join(cards.RANKS)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
