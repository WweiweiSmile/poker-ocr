#!/usr/bin/env python3
"""牌面回放金标测试：用真实样张走完整实时链路。

把 `images/card/show_card.jpg`、`images/card/showOne` 的真实识别结果按
「翻前 → 翻牌 → 转牌 → 河牌 → 下一手」喂给 `LiveProcessor`，断言：

  · 一手牌只产生一条 `hand` 记录（不能因为 4→3 动画帧被劈成 h1/h2）；
  · 公共牌只前进（state 记录里 0→3→4→5，没有回退）；
  · 摊牌 `shows`（含只秀一张）和 `all_ins` 落在同一条 hand 记录里；
  · 牌背不会被写进 shows。

需要 images/card 样张；不连 RTSP、不调模型。

    .venv/bin/python tests/test_card_replay.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import poker_ocr.live as live  # noqa: E402
from poker_ocr.cards import (  # noqa: E402
    CardSlotRead, CardsResult, recognize_cards,
)
from poker_ocr.frame_state import FrameState, PlayerState  # noqa: E402
from poker_ocr.hand_id import HAND_ID_RE  # noqa: E402
from poker_ocr.hand_track import LiveHandTracker  # noqa: E402
from poker_ocr.profile import Profile  # noqa: E402
from poker_ocr.state_stream import FrameOutcome  # noqa: E402

CARD_DIR = ROOT / "images" / "card"
SHOW_PROFILE = ROOT / "config" / "table_wpk_live.json"

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


class StubWriter:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def write(self, record: dict) -> None:
        self.rows.append(record)


class ReplayStream:
    """按预设脚本吐状态；牌面由 patched recognize_cards 提供。"""

    def __init__(self) -> None:
        self.dealers = ["d1"] * 8 + ["d2"] * 3
        self.pots = [1.5, 1.5, 7.5, 7.5, 15.5, 15.5, 35.5, 35.5, 1.5, 1.5, 1.5]
        self.all_in = [False] * 6 + [True, True] + [False] * 3
        self._n = 0
        self._last_dealer: str | None = None

    def ingest(self, img, frame_id: str, t: float, *,
               extra_signature: tuple | None = None) -> FrameOutcome:
        i = min(len(self.dealers) - 1, self._n)
        self._n += 1
        dealer = self.dealers[i]
        villain_all_in = bool(self.all_in[i])
        players = [
            PlayerState("hero", name="me", position="BTN", stack=100.0,
                        bet=0.0, in_hand=True),
            PlayerState("villain", name="V", position="BB",
                        stack=0.0 if villain_all_in else 99.0,
                        bet=20.0 if villain_all_in else 1.0, in_hand=True,
                        all_in=villain_all_in,
                        equity=30.0 if villain_all_in else None),
        ]
        state = FrameState(
            frame_id=frame_id, table_seats=2, players_in_hand=2,
            players=players, blinds=live.parse_blinds("1/2"),
            dealer=dealer, hero_seat_id="hero", hero_turn=False,
            pot=self.pots[i], pot_before_street=0.0)
        # 与真实 StateStream 一样：庄位变化 = 换手信号，live 会据此重置
        # 跨帧牌面稳定闸并重读这一帧。
        new_hand = (self._last_dealer is not None
                    and dealer != self._last_dealer)
        self._last_dealer = dealer
        record = {"index": i + 1, "t": round(t, 2), "frame": frame_id}
        return FrameOutcome("record", state, [], record, 0, 0,
                            new_hand=new_hand)

    def reset(self) -> None:      # pragma: no cover
        pass


def _slot(name: str, cards) -> CardSlotRead:
    return CardSlotRead(name=name, slot=(0, 0, 1, 1), rect=(0, 0, 10, 10),
                        cards=list(cards))


def _load_samples():
    profile = Profile.load(SHOW_PROFILE)
    show_card = recognize_cards(cv2.imread(str(CARD_DIR / "show_card.jpg")),
                                profile)
    show_one = recognize_cards(cv2.imread(str(CARD_DIR / "showOne")), profile)
    return profile, show_card, show_one


def test_replay_one_hand_and_all_artifacts() -> None:
    profile, show_card, show_one = _load_samples()
    check(show_card.hero_codes == ["Js", "Qs"],
          f"show_card hero 前提不对：{show_card.hero_codes}")
    check(show_one.board_codes == ["Kc", "6h", "3h", "Ks", "2s"],
          f"showOne board 前提不对：{show_one.board_codes}")

    hero = show_card.hero_cards
    board = show_one.board.cards
    # 每条街喂两帧：StableCards 需要连续 2 帧一致才采用。
    schedule = [
        ([], {}), ([], {}),                                # 翻前
        (board[:3], {}), (board[:3], {}),                   # 翻牌
        (board[:4], {}), (board[:4], {}),                   # 转牌
        (board[:5], show_one.shows), (board[:5], show_one.shows),  # 河牌
        # 下一手（庄位变）。换手帧 live 会重读一次牌，所以这里多留一帧。
        ([], {}), ([], {}), ([], {}), ([], {}),
    ]
    frames = iter(schedule)

    def fake_recognize(img, prof):
        cards, shows = next(frames)
        return CardsResult(size=(1080, 2400), board=_slot("board", cards),
                           hero_cards=hero, shows=dict(shows))

    writer = StubWriter()
    tracker = LiveHandTracker(confirm_frames=1)
    with patch.object(live, "recognize_cards", fake_recognize):
        proc = live.LiveProcessor(ReplayStream(), writer, quiet=True,
                                  profile=profile, hand_tracker=tracker,
                                  log=lambda *_: None)
        for seq in range(1, 12):
            # 每帧换一张不同底色的图，避免牌区 ROI 去重命中上一帧缓存
            # （真实链路里牌面变化时像素本来也会变）。
            img = np.zeros((100, 100, 3), np.uint8)
            img[:, :, 0] = (seq * 13) % 256
            proc.handle(live.FramePacket(
                seq=seq, epoch=1, grab_mono=proc.clock(), image=img))
        # 不 final=True：最后一手（h2）还没结束，不该额外产出一条 partial 记录；
        # h1 在庄位轮换那一帧已经落库。

    hands = [r for r in writer.rows if r["type"] == "hand"]
    check(len(hands) == 1,
          f"一手牌应只落一条记录，实际 {len(hands)} 条：{[h['hand_id'] for h in hands]}")
    if not hands:
        return
    hand = hands[0]
    check(HAND_ID_RE.match(str(hand["hand_id"])),
          f"hand_id 应是名单摘要而不是 h1：{hand['hand_id']}")
    check(hand["hero_cards"] == ["Js", "Qs"],
          f"hero 手牌不对：{hand['hero_cards']}")
    check(hand["board"] == ["Kc", "6h", "3h", "Ks", "2s"],
          f"公共牌应只前进到河牌：{hand['board']}")

    shows = {s["seat_id"]: s["cards"] for s in hand.get("shows", [])}
    check(shows.get("lowR") == ["Kd"],
          f"只秀一张应只记 1 张、牌背不能凑数：{shows}")
    check(shows.get("upR") == ["6d"], f"upR 应只记一张：{shows}")
    check(shows.get("midL") == ["Ac", "Qh"], f"midL 应记两张：{shows}")
    check(all(len(cards) in (1, 2) for cards in shows.values()),
          f"shows 只能是 1–2 张：{shows}")

    all_ins = {a["seat_id"]: a for a in hand.get("all_ins", [])}
    check(all_ins.get("villain", {}).get("equity") == 30.0,
          f"全下胜率应落在同一条 hand 记录：{all_ins}")

    # state 记录里的公共牌张数只能前进，不能出现 4→3 / 5→4；
    # 允许 5→0（新一手开始）。
    lengths = [len(r["board"]) for r in writer.rows if r["type"] == "state"]
    monotonic = all(b >= a or b == 0 for a, b in zip(lengths, lengths[1:]))
    check(monotonic, f"state 记录的公共牌出现回退：{lengths}")
    check(max(lengths or [0]) == 5, f"应记录到 5 张公共牌：{lengths}")


def main() -> int:
    tests = [test_replay_one_hand_and_all_artifacts]
    for t in tests:
        before = len(FAILS)
        try:
            t()
        except Exception as exc:                      # noqa: BLE001
            FAILS.append(f"{t.__name__} 抛异常：{type(exc).__name__}: {exc}")
        print(f"  {'❌' if len(FAILS) > before else '✅'} {t.__name__}")

    if FAILS:
        print(f"\n{len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("\n✅ 牌面回放金标全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
