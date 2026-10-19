#!/usr/bin/env python3
"""公共牌 / hero 手牌识别的回归测试。

用 images/card 下 16 个标注样本（15 张唯一画面 + redA）逐张校验：
公共牌、hero 手牌、以及「颜色与形状两条证据是否一致」。

这些样本覆盖了识别最容易翻车的几种状态：
  * 三/四/五张公共牌（居中对齐，位置会变）
  * hero 弃牌后牌面变灰（V≈103，不是白色）
  * 翻前没有公共牌、且被活动弹窗盖住
  * 同一手牌的多个街、同一张图的两个副本

不依赖 pytest，直接跑：
    .venv/bin/python tests/test_cards.py
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.cards import (  # noqa: E402
    RANKS, recognize_cards, recognize_cards_of_file,
)
from poker_ocr.profile import Profile  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


CARD_DIR = ROOT / "images" / "card"
PROFILE_PATH = ROOT / "config" / "table_wpk_1080x2400.json"
DEFAULT_PROFILE_PATH = ROOT / "config" / "table_default.json"

# key -> (公共牌, hero 手牌)，均按画面从左到右。
# key 取文件名里的序号；redA.jpg 单独命名。
GROUND_TRUTH: dict[str, tuple[list[str], list[str]]] = {
    "138": (["3d", "Qc", "Ad"], ["5h", "Ks"]),
    "139": (["3d", "Qc", "Ad"], ["5h", "Ks"]),
    "140": (["Ac", "6h", "4s", "10d"], ["2c", "8s"]),
    "141": (["Kc", "9s", "6c"], ["4h", "Qs"]),
    "142": (["5d", "2h", "Jc", "Kc"], ["2c", "6c"]),
    "143": (["Kc", "3s", "Qd"], ["Kh", "3h"]),
    "144": (["5h", "Qd", "Jd"], ["9d", "Qh"]),
    "145": ([], ["9d", "Qh"]),
    "146": (["9h", "8s", "Ad", "Jh", "10s"], ["Js", "5c"]),
    "147": (["9h", "8s", "Ad", "Jh"], ["Js", "5c"]),
    "148": (["9h", "8s", "Ad"], ["Js", "5c"]),
    "149": ([], ["Kd", "8h"]),
    "150": (["5s", "2d", "Kc"], ["9s", "10d"]),
    "151": (["5s", "2d", "Kc"], ["9s", "10d"]),
    "152": (["7d", "9c", "10c", "6h", "8c"], ["4c", "Ks"]),
    "redA": (["3s", "2h", "5s"], ["Ah", "Qd"]),
}


def _key(path: Path) -> str:
    if path.stem.startswith("redA"):
        return "redA"
    return path.stem.split("_")[2]


def _unique_files() -> dict[str, Path]:
    """同内容只留一份（138/139 各有一个 (1) 副本）。"""
    seen: dict[str, str] = {}
    out: dict[str, Path] = {}
    for path in sorted(CARD_DIR.glob("*.jpg")):
        key = _key(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in seen:
            continue
        seen[digest] = key
        out[key] = path
    return out


def test_all_labeled_samples() -> None:
    """逐张比对公共牌与 hero 手牌。"""
    profile = Profile.load(PROFILE_PATH)
    files = _unique_files()
    check(set(files) == set(GROUND_TRUTH),
          f"样本集合与标注不一致：多 {set(files) - set(GROUND_TRUTH)}，"
          f"少 {set(GROUND_TRUTH) - set(files)}")

    for key, path in sorted(files.items()):
        img = cv2.imread(str(path))
        check(img is not None, f"{key}: 读不了 {path}")
        if img is None:
            continue
        result = recognize_cards(img, profile)
        exp_board, exp_hero = GROUND_TRUTH[key]
        check(result.board_codes == exp_board,
              f"{key} 公共牌：得到 {result.board_codes}，期望 {exp_board}")
        check(result.hero_codes == exp_hero,
              f"{key} hero 手牌：得到 {result.hero_codes}，期望 {exp_hero}")


def test_color_and_shape_agree() -> None:
    """所有识别出的牌，颜色判定的花色应与形状判定一致。

    这是「颜色 + 形状」双证据的核心断言：只要有一条不一致，说明形状规则或
    颜色阈值有一边漂了，应该立刻看到，而不是被平均进置信度里。
    """
    profile = Profile.load(PROFILE_PATH)
    for key, path in sorted(_unique_files().items()):
        for slot_name in ("board", "hero_cards"):
            result = recognize_cards_of_file(path, profile)
            slot = result.board if slot_name == "board" else result.hero_cards
            for card in slot.cards:
                check(card.evidence.get("shape_agrees") is True,
                      f"{key} {slot_name} {card.code}: 颜色/形状不一致 "
                      f"{card.evidence.get('shape_suit')}")


def test_default_profile_falls_back_to_wpk_slots() -> None:
    """没有 board/hero_cards 槽位的 profile 也要能读 —— API 默认就是这种。

    api.py 默认加载 table_default.json（浏览器版式），但同一批 1080x2400 截图
    仍应能读牌；此时走 DEFAULT_CARD_SLOTS 的归一化坐标。
    """
    default_profile = Profile.load(DEFAULT_PROFILE_PATH)
    check(not default_profile.global_slots.get("board"),
          "前提不成立：table_default.json 竟然有 board 槽位")

    key = "138"
    result = recognize_cards_of_file(_unique_files()[key], default_profile)
    check(result.board_codes == GROUND_TRUTH[key][0],
          f"默认 profile 回退后公共牌：得到 {result.board_codes}")
    check(result.hero_codes == GROUND_TRUTH[key][1],
          f"默认 profile 回退后 hero：得到 {result.hero_codes}")
    check(any("默认牌区" in w for w in result.warnings),
          "回退到默认牌区时应该给出 warning")


def test_preflop_has_no_false_positive() -> None:
    """翻前（无公共牌、被活动弹窗盖住）必须返回空，不能硬凑出牌。"""
    profile = Profile.load(PROFILE_PATH)
    for key in ("145", "149"):
        result = recognize_cards_of_file(_unique_files()[key], profile)
        check(result.board_codes == [],
              f"{key} 翻前不应识别出公共牌，得到 {result.board_codes}")


def test_all_ranks_have_templates() -> None:
    from poker_ocr.cards import _load_templates
    templates = _load_templates()
    check(set(templates) == set(RANKS),
          f"模板缺项：{set(RANKS) - set(templates)}")


def main() -> int:
    tests = [
        test_all_ranks_have_templates,
        test_all_labeled_samples,
        test_color_and_shape_agree,
        test_default_profile_falls_back_to_wpk_slots,
        test_preflop_has_no_false_positive,
    ]
    for t in tests:
        before = len(FAILS)
        t()
        print(f"  {'❌' if len(FAILS) > before else '✅'} {t.__name__}")

    if FAILS:
        print(f"\n{len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("\n✅ 公共牌 / hero 手牌识别全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
