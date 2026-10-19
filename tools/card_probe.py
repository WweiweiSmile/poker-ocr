#!/usr/bin/env python3
"""单图读牌：识别公共牌与 hero 手牌。

    .venv/bin/python tools/card_probe.py images/card/redA.jpg
    .venv/bin/python tools/card_probe.py shot.jpg --debug

默认输出紧凑 JSON：{"board": [...], "hero_cards": [...]}。
--debug 追加逐张的像素框、花色/形状证据、点数模板分数 —— 排查误识别时用。

不指定 --profile 时用 WPK 移动端默认牌区（与 config/table_wpk_1080x2400.json
的 global_slots 一致）；指定则优先用 profile 里的槽位。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.cards import recognize_cards_of_file  # noqa: E402
from poker_ocr.profile import Profile  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", type=Path, help="牌桌截图")
    ap.add_argument("--profile", type=Path, default=None,
                    help="牌桌几何配置；不指定则用内置 WPK 牌区")
    ap.add_argument("--debug", action="store_true", help="输出逐张证据")
    args = ap.parse_args()

    if not args.image.exists():
        raise SystemExit(f"找不到图片：{args.image}")

    profile = Profile.load(args.profile) if args.profile else None
    result = recognize_cards_of_file(args.image, profile)
    print(json.dumps(result.to_dict(include_details=args.debug),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
