#!/usr/bin/env python3
"""StateStream 的单测：防抖、丢弃、消失确认、断流重置。

不加载模型、不读图片：把 `RoiStream` / `read_avatars` / `build_detector`
换成桩，帧用 `test_frame_state.make_frame` 手工拼。真正跑 OCR 的等价性
由 `tools/video_to_states.py` 对 video1 的离线回归保证。

    .venv/bin/python tests/test_state_stream.py
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from dataclasses import replace as dc_replace
from pathlib import Path
from unittest.mock import patch

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import poker_ocr.state_stream as ss  # noqa: E402
from poker_ocr.parse import Blinds  # noqa: E402
from test_frame_state import BASE, PROFILE, make_frame  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


class StubRoiStream:
    """按顺序吐出预置的 RoiFrame；只记录 reset 次数。"""

    def __init__(self, profile, engine, detector=None, **kwargs) -> None:
        self.cache = None
        self.frames = []
        self.resets = 0

    def read(self, img, frame_id: str = "frame", *, detect_dealer: bool = True):
        if not self.frames:
            raise AssertionError("StubRoiStream 没有待处理的帧")
        return dc_replace(self.frames.pop(0), frame_id=frame_id)

    def reset(self) -> None:
        self.resets += 1


@contextmanager
def stub_env():
    with patch.object(ss, "RoiStream", StubRoiStream), \
            patch.object(ss, "build_detector", lambda *a, **k: None), \
            patch.object(ss, "read_avatars", lambda *a, **k: {}), \
            patch.object(ss, "read_slots", lambda *a, **k: []):
        yield


def new_stream(**kwargs) -> ss.StateStream:
    kwargs.setdefault("detect_fold", False)
    return ss.StateStream(PROFILE, engine=object(), **kwargs)


@contextmanager
def plus_seats(*seat_ids: str):
    """把 `read_empty_seats` 换成「这些座位是 + 占位」的桩。"""
    with patch.object(ss, "read_empty_seats",
                      lambda *a, **k: {sid: True for sid in seat_ids}):
        yield


def feed(stream: ss.StateStream, frame, t: float = 0.0,
         extra_signature: tuple | None = None) -> ss.FrameOutcome:
    stream.stream.frames.append(frame)
    return stream.ingest(np.zeros((4, 4, 3), np.uint8), f"f{int(t * 100):04d}", t,
                         extra_signature=extra_signature)


# ─────────────────────────── 用例 ───────────────────────────

def test_first_frame_is_baseline() -> None:
    with stub_env():
        s = new_stream()
        out = feed(s, make_frame())
        check(out.kind == "record", f"首帧应记录，实际 {out.kind}")
        check(out.record is not None and out.record["index"] == 1, "首帧 index 应为 1")
        check(out.record["pot"] == 38.0, f"底池应为 38，实际 {out.record['pot']}")


def test_same_signature_is_not_recorded_again() -> None:
    with stub_env():
        s = new_stream()
        feed(s, make_frame())
        out = feed(s, make_frame(), t=0.5)
        check(out.kind == "no_change", f"同状态应无变化，实际 {out.kind}")
        check(s.summary()["record_count"] == 1, "只应记录一次")


def test_change_needs_two_stable_frames() -> None:
    with stub_env():
        s = new_stream(stable_frames=2)
        feed(s, make_frame())
        changed = make_frame(pot="40BB")
        first = feed(s, changed, t=0.5)
        second = feed(s, changed, t=1.0)
        check(first.kind == "no_change", f"变化第一帧还不该记录，实际 {first.kind}")
        check(second.kind == "record", f"变化第二帧应记录，实际 {second.kind}")
        check(second.record["pot"] == 40.0, "记录的应是新底池")


def test_single_stable_frame_records_immediately() -> None:
    """stable_frames=1：变化第一帧就记录，不再等第二帧。"""
    with stub_env():
        s = new_stream(stable_frames=1)
        feed(s, make_frame())
        changed = make_frame(pot="40BB")
        out = feed(s, changed, t=0.5)
        check(out.kind == "record", f"1 帧确认应立即记录，实际 {out.kind}")
        check(out.record["pot"] == 40.0, "记录的应是新底池")


def test_discard_does_not_advance_prev() -> None:
    with stub_env():
        s = new_stream()
        baseline = make_frame()
        feed(s, baseline)
        bad = feed(s, make_frame(pot=""), t=0.5)
        check(bad.kind == "discard", f"底池读不出应丢弃，实际 {bad.kind}")
        check("pot" in bad.errors, f"丢弃原因应含 pot：{bad.errors}")
        # 被丢弃的帧不能推进 prev；回到原状态仍应判「无变化」
        out = feed(s, baseline, t=1.0)
        check(out.kind == "no_change", f"回到基线应无变化，实际 {out.kind}")
        check(s.summary()["discarded_reasons"].get("pot") == 1, "pot 丢弃计数应为 1")


def test_vanished_seat_needs_max_vanish_frames() -> None:
    with stub_env():
        s = new_stream(max_vanish=3)
        feed(s, make_frame())
        # 用 midL 而不是 lowR：lowR 是基线里的全下玩家，会走 all-in 继承，
        # 不会被判成离座。midL 变成「筹码 0、无下注、无动作」= sitting_out。
        rows = {k: dict(v) for k, v in BASE.items()}
        rows["midL"] = {"name": "伯克利丁真", "chip": "0", "bet": ""}
        gone = make_frame(rows)
        outs = [feed(s, gone, t=0.5 * (i + 1)) for i in range(5)]
        check([o.kind for o in outs[:3]] == ["discard"] * 3,
              f"前 3 帧应因 vanished 丢弃：{[o.kind for o in outs]}")
        check(outs[3].kind == "no_change", f"第 4 帧应接受，实际 {outs[3].kind}")
        check(outs[4].kind == "record", f"第 5 帧应记录，实际 {outs[4].kind}")
        check(s.summary()["discarded_reasons"].get("vanished") == 3,
              f"vanished 计数应为 3：{s.summary()['discarded_reasons']}")


def test_departed_seat_with_chip_garbage_recovers() -> None:
    """人走后空座的 chip 槽读成乱码时，也要在 max_vanish 帧后恢复识别。

    没有换手边界（离线/不传 extra_signature）时保持旧口径：空座从 players
    里消失。实时链路会锁定 roster、保留名字/位置，见下面两个用例。
    """
    with stub_env():
        s = new_stream(max_vanish=3)
        feed(s, make_frame())
        rows = {k: dict(v) for k, v in BASE.items()}
        rows["lowL"] = {"name": "", "chip": "--", "bet": ""}
        gone = make_frame(rows)
        outs = [feed(s, gone, t=0.5 * (i + 1)) for i in range(5)]
        check([o.kind for o in outs[:3]] == ["discard"] * 3,
              f"前 3 帧应因 vanished 丢弃：{[o.kind for o in outs]}")
        check(all("stack:lowL" not in o.errors for o in outs),
              f"空座乱码不能报 stack：{[o.errors for o in outs]}")
        check(outs[3].kind == "no_change", f"第 4 帧应接受，实际 {outs[3].kind}")
        check(outs[4].kind == "record", f"第 5 帧应记录，实际 {outs[4].kind}")
        check(outs[4].state.players_in_hand == 8,
              f"在场人数应为 8，实际 {outs[4].state.players_in_hand}")
        check(outs[4].state.player("lowL").name is None,
              "退出的座位不能留下旧名字")
        check(s.summary()["discarded_reasons"].get("vanished") == 3,
              f"vanished 计数应为 3：{s.summary()['discarded_reasons']}")


def test_exit_mid_hand_keeps_name_position_and_marks_fold() -> None:
    """9→8 人：中途退出的人本手按弃牌处理，名字/位置保留，其余人不挪位。

    实盘失败形态：非庄位玩家退出后按 8 人重新编号，空位后面的人整体前移
    （hero UTG+2→LJ），行动顺序和 AI 上下文全错；同时空座的旧名字/筹码
    抖动会让质量闸每帧报 stack，识别停摆。
    """
    with stub_env():
        s = new_stream(max_vanish=3)
        sig = (("As", "Kd"), ())
        first = feed(s, make_frame(), extra_signature=sig)
        check(first.kind == "record", f"首帧应记录，实际 {first.kind}")
        check(s.summary()["hand_roster"] == 9,
              f"应锁定 9 个座位：{s.summary()['hand_roster']}")
        base_pos = {p.seat_id: p.position for p in first.state.players}

        # 离桌只认头像「+」占位（正向证据）：连续 2 帧确认后按弃牌处理。
        with plus_seats("midL"):
            outs = [feed(s, make_frame(), t=0.5 * (i + 1), extra_signature=sig)
                    for i in range(3)]
        state = outs[-1].state
        mid = state.player("midL")
        check(mid.left and mid.in_hand and mid.fold is True
              and mid.status == "folded",
              f"离桌应保留在 players 里并按弃牌算：{mid.to_debug()}")
        check(mid.name == BASE["midL"]["name"],
              f"离桌后应沿用名字，实际 {mid.name!r}")
        check(mid.position == "HJ",
              f"离桌后应保留位置 HJ，实际 {mid.position!r}")
        check(mid.stack is None and mid.bet == 0.0,
              f"离桌后不该要求筹码/下注：{mid.to_debug()}")
        check(state.players_in_hand == 9,
              f"这一手仍是 9 人局（离桌按弃牌），实际 {state.players_in_hand}")
        check(not state.errors, f"确认离桌后不该再丢帧：{state.errors}")
        check({p.seat_id: p.position for p in state.players} == base_pos,
              "有人退出不能让其他人的位置整体挪位："
              f"{ {p.seat_id: p.position for p in state.players} }")
        check(s.summary()["hand_left"] == ["midL"],
              f"离桌集合不对：{s.summary()['hand_left']}")

        # 下一手：roster 重建，空位不再算进这一手。
        rows = {k: dict(v) for k, v in BASE.items()}
        rows["midL"] = {"name": "", "chip": "", "bet": ""}
        out = feed(s, make_frame(rows), t=3.5,
                   extra_signature=(("Qs", "Jd"), ()))
        check(out.new_hand, "换手应被识别")
        seat = out.state.player("midL")
        check(not seat.in_hand and not seat.left and seat.name is None,
              f"下一手空座应重新识别：{seat.to_debug()}")
        check(out.state.players_in_hand == 8,
              f"下一手 8 人局，实际 {out.state.players_in_hand}")


def test_new_player_mid_hand_kept_out_until_next_hand() -> None:
    """本手中途坐下的人：这手继续用上一位玩家的信息，下一手再识别新玩家。"""
    with stub_env():
        s = new_stream(max_vanish=3)
        sig = (("As", "Kd"), ())
        feed(s, make_frame(), extra_signature=sig)

        # 原玩家 midL 离开：头像「+」占位连续 2 帧确认。
        with plus_seats("midL"):
            for i in range(2):
                feed(s, make_frame(), t=0.5 * (i + 1), extra_signature=sig)
        check("midL" in s.hand_left, "midL 应先被确认离桌")

        # 新玩家坐进同一个座位：本手仍是上一位的信息。
        rows = {k: dict(v) for k, v in BASE.items()}
        rows["midL"] = {"name": "新玩家", "chip": "150BB", "bet": ""}
        new_player = make_frame(rows)
        for i in range(3):
            out = feed(s, new_player, t=3.0 + 0.5 * i, extra_signature=sig)
        seat = out.state.player("midL")
        check(seat.left and seat.fold is True and seat.in_hand,
              f"本手应继续按离桌/弃牌处理：{seat.to_debug()}")
        check(seat.name == BASE["midL"]["name"],
              f"本手应保留上一位玩家的名字，实际 {seat.name!r}")
        check(seat.position == "HJ",
              f"本手应保留上一位玩家的位置，实际 {seat.position!r}")
        check(seat.stack is None,
              f"新玩家的筹码不该写进这一手：{seat.stack!r}")
        check(out.state.players_in_hand == 9,
              f"本手人数不变，实际 {out.state.players_in_hand}")

        # 下一手才识别新玩家。
        out = feed(s, new_player, t=5.0, extra_signature=(("Qs", "Jd"), ()))
        seat = out.state.player("midL")
        check(out.new_hand, "换手应被识别")
        check(seat.in_hand and not seat.left and seat.name == "新玩家",
              f"下一手应识别新玩家：{seat.to_debug()}")
        check(seat.stack == 150.0,
              f"下一手应读新玩家的筹码，实际 {seat.stack!r}")
        check(seat.position == "HJ",
              f"新玩家的位置应正常排出，实际 {seat.position!r}")


def test_occluded_player_is_not_marked_left() -> None:
    """整座被弹窗盖住（三块槽位全空、没有「+」占位）**不算离桌**。

    名单锁定的座位默认在局；这一帧读不到只说明遮挡，质量闸按 stack/bet
    缺失丢帧，状态和位置都不变。这样弹窗/动画不会把人误判成弃牌。
    """
    with stub_env():
        s = new_stream(max_vanish=2, detect_fold=True)
        sig = (("As", "Kd"), ())
        feed(s, make_frame(), extra_signature=sig)

        rows = {k: dict(v) for k, v in BASE.items()}
        rows["midL"] = {"name": "", "chip": "", "bet": ""}
        occluded = make_frame(rows)
        outs = [feed(s, occluded, t=0.5 * (i + 1), extra_signature=sig)
                for i in range(4)]
        for out in outs:
            check(out.kind == "discard" and "stack:midL" in out.errors,
                  f"遮挡帧应因 stack 缺失丢弃：{out.kind} / {out.errors}")
            mid = out.state.player("midL")
            check(mid.in_hand and not mid.left and mid.fold is not True,
                  f"遮挡不能判离桌/弃牌：{mid.to_debug()}")
        check("midL" not in s.summary()["hand_left"],
              f"离桌集合不该有他：{s.summary()['hand_left']}")

        # 遮挡消失：正常帧恢复记录，位置/名字都不变。
        out = feed(s, make_frame(), t=3.0, extra_signature=sig)
        mid = out.state.player("midL")
        check(mid.in_hand and not mid.left and mid.position == "HJ",
              f"遮挡结束后应恢复：{mid.to_debug()}")


def test_plus_placeholder_marks_left_and_icon_return_revives() -> None:
    """「+」占位确认离桌后，手牌图标回来（占位是误判）要撤销 left。"""
    icon_on = {seat.seat_id: True for seat in PROFILE.seats}
    with stub_env():
        s = new_stream(max_vanish=2, detect_fold=True)
        sig = (("As", "Kd"), ())
        with patch.object(ss, "read_card_icons", lambda *a, **k: dict(icon_on)):
            feed(s, make_frame(), extra_signature=sig)
        with plus_seats("midL"), patch.object(
                ss, "read_card_icons", lambda *a, **k: dict(icon_on)):
            for i in range(2):
                feed(s, make_frame(), t=0.5 * (i + 1), extra_signature=sig)
        check("midL" in s.hand_left, f"「+」占位应确认离桌：{s.summary()}")

        with patch.object(ss, "read_card_icons", lambda *a, **k: dict(icon_on)):
            out = feed(s, make_frame(), t=3.0, extra_signature=sig)
        mid = out.state.player("midL")
        check(not mid.left and mid.in_hand and mid.fold is not True,
              f"图标回来后应撤销离桌判定：{mid.to_debug()}")
        check("midL" not in s.summary()["hand_left"],
              f"离桌集合应清掉：{s.summary()['hand_left']}")


def test_lineup_change_between_hands_does_not_discard() -> None:
    """换手时座位空出来是正常换人，不能当成本手「整座消失」丢帧。"""
    with stub_env():
        s = new_stream(max_vanish=3)
        feed(s, make_frame(), extra_signature=(("As", "Kd"), ()))
        rows = {k: dict(v) for k, v in BASE.items()}
        rows["midL"] = {"name": "", "chip": "", "bet": ""}
        out = feed(s, make_frame(rows), t=1.0,
                   extra_signature=(("Qs", "Jd"), ()))
        check(out.new_hand, "换手应被识别")
        check(out.kind != "discard" and not out.errors,
              f"换人后的新手第一帧不该丢：{out.kind} / {out.errors}")
        check(out.state.players_in_hand == 8,
              f"新手应为 8 人局，实际 {out.state.players_in_hand}")
        check(not out.state.player("midL").left,
              "换手后空座是新阵容，不应标成本手离桌")


def test_dead_button_dealer_left_keeps_recording() -> None:
    """庄家中途退出：按钮留在他身上时不能整段丢帧（2026-10-23 实盘）。"""
    with stub_env():
        s = new_stream()
        rows = {k: dict(v) for k, v in BASE.items()}
        rows["topR"] = {"name": "", "chip": "", "bet": ""}
        out = feed(s, make_frame(rows, dealer="topR"))
        check(out.kind == "record",
              f"死按钮首帧应记录，实际 {out.kind} / {out.errors}")
        check(not out.errors, f"死按钮不该进质量闸：{out.errors}")
        check(out.state.player("hero").position == "UTG+1",
              f"hero 位置应按死按钮排：{out.state.player('hero').to_debug()}")
        check(out.state.player("lowL").position == "UTG+2",
              f"lowL 不能因庄家退出往前挪：{out.state.player('lowL').to_debug()}")
        check(out.state.player("topR").position is None,
              "退出的庄家不该有位置")


def test_initial_blinds_carried_when_banner_unreadable() -> None:
    with stub_env():
        s = new_stream(initial_blinds=Blinds(0.5, 1.0, None, "1/2"))
        out = feed(s, make_frame(blinds=""))
        check("blinds" not in out.errors,
              f"有初始盲注时不该因 blinds 丢弃：{out.errors}")
        check(out.state.blinds is not None and out.state.blinds.sb == 0.5,
              f"应继承初始盲注，实际 {out.state.blinds}")
        check(out.state.blinds_source == "carried",
              f"盲注来源应为 carried，实际 {out.state.blinds_source}")


def test_reset_makes_next_frame_a_new_baseline() -> None:
    with stub_env():
        s = new_stream()
        feed(s, make_frame())
        s.reset()
        out = feed(s, make_frame(), t=0.5)
        check(out.kind == "record", f"重置后首帧应记录，实际 {out.kind}")
        check(out.record["index"] == 2, "重置后 index 应继续递增")
        check(s.stream.resets == 1, "RoiStream.reset 应被调用一次")


def test_dealer_rotation_clears_all_in_carry() -> None:
    with stub_env():
        s = new_stream()
        feed(s, make_frame())
        check(s.prev_all_in == {"lowR"}, f"基线应记住 lowR 全下：{s.prev_all_in}")
        rows = {k: dict(v) for k, v in BASE.items()}
        rows.pop("lowR")
        feed(s, make_frame(rows, dealer="topR"), t=0.5)
        check(s.prev_dealer == "topR", f"prev_dealer 应更新，实际 {s.prev_dealer}")
        check(s.prev_all_in == set(),
              f"庄位轮转应清空全下继承，实际 {s.prev_all_in}")


def test_all_in_badge_disappearance_ends_hand() -> None:
    """头像 All-in 徽章连续 2 帧变成可读的非 All-in 文本 = 这手结束。"""
    reads = [{"hero": ("Alin", 0.7)}, {"hero": ("Alin", 0.7)},
             {"hero": ("弃牌", 0.9)}, {"hero": ("弃牌", 0.9)}]
    with stub_env(), patch.object(ss, "read_avatars",
                                  lambda *a, **k: reads.pop(0)):
        s = new_stream()
        first = feed(s, make_frame())
        check(not first.hand_end, "刚读到 All-in 不该判换手")
        check(s.prev_all_in_badges == {"hero"},
              f"应记住 hero 全下：{s.prev_all_in_badges}")
        second = feed(s, make_frame(), t=0.5)
        check(not second.hand_end, "徽章还在时不该判换手")
        third = feed(s, make_frame(), t=1.0)
        check(not third.hand_end, "单帧消失先不判，防遮挡/幻觉")
        fourth = feed(s, make_frame(), t=1.5)
        check(fourth.hand_end, "连续两帧消失应判这手结束")
        check(s.prev_all_in_badges == set(),
              f"换手后应清空全下徽章集合：{s.prev_all_in_badges}")


def test_all_in_badge_disappears_during_hero_turn_is_not_hand_end() -> None:
    """还轮到 hero 行动时，All-in 徽章消失不能收手（按钮/照片误读）。"""
    reads = [{"hero": ("Alin", 0.7)}, {"hero": ("Alin", 0.7)},
             {"hero": ("弃牌", 0.9)}, {"hero": ("弃牌", 0.9)}]
    with stub_env(), patch.object(ss, "read_avatars",
                                  lambda *a, **k: reads.pop(0)):
        s = new_stream()
        for i in range(4):
            out = feed(s, make_frame(hero_turn=True), t=0.5 * i)
        check(not out.hand_end,
              "hero 还在行动时不能凭 All-in 徽章消失收手")


def test_all_in_badge_end_waits_for_river() -> None:
    """全下后公共牌还没发完，All-in 徽章消失不能提前收手。"""
    reads = [{"hero": ("Alin", 0.7)}, {"hero": ("Alin", 0.7)},
             {"hero": ("弃牌", 0.9)}, {"hero": ("弃牌", 0.9)},
             {"hero": ("弃牌", 0.9)}]
    with stub_env(), patch.object(ss, "read_avatars",
                                  lambda *a, **k: reads.pop(0)):
        s = new_stream()

        def feed_board(board):
            s.stream.frames.append(make_frame(hero_turn=False))
            return s.ingest(np.zeros((4, 4, 3), np.uint8), frame_id="f", t=0.0,
                            extra_signature=(("Ah", "Kd"), tuple(board)))

        turn = ["Ks", "7d", "2c", "Qh"]
        feed_board(turn)                 # Alin
        feed_board(turn)                 # Alin
        feed_board(turn)                 # 消失第 1 帧
        out = feed_board(turn)           # 消失第 2 帧；turn 阶段不能收手
        check(not out.hand_end, "turn 阶段不能凭徽章消失收手")
        river = feed_board(turn + ["9s"])  # 河牌到齐
        check(river.hand_end, "河牌到齐后应收手")


class FakeCache:
    """只实现钉住相关的方法；StateStream 的慢变量缓存只用到这几个。"""

    def __init__(self) -> None:
        self.pinned: set[str] = set()

    def is_pinned(self, key: str) -> bool:
        return key in self.pinned

    def pin(self, key: str) -> bool:
        self.pinned.add(key)
        return True

    def unpin(self, key: str) -> None:
        self.pinned.discard(key)

    def unpin_all(self) -> None:
        self.pinned.clear()


def test_hand_boundaries_from_cards_and_board() -> None:
    """live 的 extra_signature：牌面掉回 0 / hero 换两张牌 = 新一手。"""
    with stub_env():
        s = new_stream()
        check(not s._hand_changed((("Ah", "Kd"), ("Ks", "7d", "2c"))),
              "首帧不该判成换手")
        check(not s._hand_changed((("Ah", "Kd"), ())),
              "牌面刚读空的第一帧先不判换手（可能是遮挡）")
        check(s._hand_changed((("Ah", "Kd"), ())),
              "连续两帧空牌面才判换手")
        check(not s._hand_changed((("Ah", "Kd"), ())), "空牌面重复不是换手")
        check(s._hand_changed((("Qs", "Jd"), ())), "hero 换了两张牌应判换手")
        check(not s._hand_changed(None), "没有 live 信号时不做换手推断")


def test_slow_fields_pinned_until_hand_switch() -> None:
    """名字/盲注读到可信值就钉住；动作徽章不钉；换手整批解钉。

    名字**要先被稳定器确认**才钉：钉住等于这一手不再重读，一帧错读就再也
    纠正不了（2026-11-01 实盘整手牌都错）。所以这里先喂 3 帧再测钉图。
    """
    from poker_ocr.roi_reader import RoiFrame, RoiRead, SeatRoi

    with stub_env():
        s = new_stream(name_confirm=3)
        s.stream.cache = FakeCache()
        frame = RoiFrame(
            frame_id="f", size=(100, 100), hero_seat_id="hero",
            seats=[SeatRoi("hero", 0,
                           {"name": RoiRead("name", (0, 0, 1, 1),
                                            [("Alice", 0.95)])})],
            globals={"blinds": RoiRead("blinds", (0, 0, 1, 1),
                                       [("1/2(）30301600:24:28", 0.9)])})
        # 还没确认（只读到一帧）→ 不钉，下一帧还能重读、纠正
        s.names.resolve("hero", "Alice")
        s._pin_slow_fields(frame)
        check("hero.name" not in s.stream.cache.pinned,
              f"没确认的名字不该钉住：{s.stream.cache.pinned}")

        for _ in range(3):
            s.names.resolve("hero", "Alice")
        check(s.names.confirmed("hero"), "3 帧之后应算确认")
        s._pin_slow_fields(frame)
        check("hero.name" in s.stream.cache.pinned,
              f"可信名字应钉住：{s.stream.cache.pinned}")
        check("@blinds" in s.stream.cache.pinned,
              f"可解析的盲注应钉住：{s.stream.cache.pinned}")

        # 动作徽章盖住名字槽时不能当名字钉住；盲注读坏也不钉
        s.stream.cache.unpin_all()
        frame.seats[0].fields["name"] = RoiRead(
            "name", (0, 0, 1, 1), [("弃牌", 0.95)])
        frame.globals["blinds"] = RoiRead("blinds", (0, 0, 1, 1), [])
        s._pin_slow_fields(frame)
        check("hero.name" not in s.stream.cache.pinned,
              f"动作徽章不该钉成名字：{s.stream.cache.pinned}")
        check("@blinds" not in s.stream.cache.pinned,
              f"读不出盲注不该钉：{s.stream.cache.pinned}")

        s.stream.cache.pinned.add("hero.name")
        s._restart_sticky()
        check(not s.stream.cache.pinned, "换手应解钉全部慢变量")


def test_name_pin_expires_so_action_badges_can_be_read() -> None:
    """名字钉住必须有期限：动作徽章（让牌/下注）就画在名字槽上。

    钉死整手 = 这一手所有徽章都读不到，行动线只能靠街末底池对账补
    inferred（2026-11-08 实盘 18 条动作 14 条 inferred；video1 上开钉住
    120 帧读到 0 个徽章，关钉住读到 69 个）。
    """
    from poker_ocr.roi_reader import RoiFrame, RoiRead, SeatRoi

    with stub_env():
        s = new_stream(name_confirm=1)
        s.stream.cache = FakeCache()
        frame = RoiFrame(
            frame_id="f", size=(100, 100), hero_seat_id="hero",
            seats=[SeatRoi("hero", 0,
                           {"name": RoiRead("name", (0, 0, 1, 1),
                                            [("Alice", 0.95)])})],
            globals={})
        s.names.resolve("hero", "Alice")
        s.sampled = 1
        s._pin_slow_fields(frame)
        check("hero.name" in s.stream.cache.pinned, "可信名字应钉住")

        s.sampled = 2
        s._age_name_pins()
        check("hero.name" in s.stream.cache.pinned, "刚钉住不该下一帧就解钉")

        s.sampled = 3
        s._age_name_pins()
        check("hero.name" not in s.stream.cache.pinned,
              f"钉住到期应解钉，实际 {s.stream.cache.pinned}")
        check(not s._name_pinned_at, "解钉后不该留时间戳")

        # 盲注不进这套到期逻辑：它整手钉住，没有徽章会盖在上面
        s.stream.cache.pinned.add("@blinds")
        s.sampled = 9
        s._age_name_pins()
        check("@blinds" in s.stream.cache.pinned, "盲注不该被名字到期一起解钉")


def test_bomb_banner_latches_across_frames() -> None:
    """横幅只在开局闪一下：即使横幅帧被丢弃，本手后续状态也要带标记。"""
    with stub_env():
        s = new_stream()
        feed(s, make_frame())
        # 开局下注动画帧：底池读不出 → 整帧丢弃，但横幅本身读到了
        bad = feed(s, make_frame(pot="", bomb_en="BOMB POT"), t=0.5)
        check(bad.kind == "discard", f"这帧应因底池丢弃，实际 {bad.kind}")
        check(bad.state.bomb_pot, "丢弃帧也要保留本帧的横幅读数")
        # 横幅已经消失，但同一手后面的状态仍应带标记
        out = feed(s, make_frame(), t=1.0)
        check(out.state.bomb_pot, "同一手内应一直保留爆炸标记")

        # 过几帧后正常换手（庄位轮换）→ 清空，不带到下一手
        for i in range(4):
            feed(s, make_frame(), t=1.5 + 0.5 * i)
        nxt = feed(s, make_frame(dealer="topR"), t=4.0)
        check(not nxt.state.bomb_pot, "换手后应清空爆炸标记")


def test_card_icon_fold_needs_two_absent_frames() -> None:
    """图标消失要连续 2 帧才判弃牌；亮牌座位没有图标也算在池。"""
    with stub_env():
        s = new_stream(detect_fold=True)
        img = np.zeros((4, 4, 3), np.uint8)
        with patch.object(ss, "read_card_icons",
                          lambda _img, _p: {"topR": False, "lowL": True}):
            first = s._read_icons(img)
            second = s._read_icons(img)
            check(first["topR"] is None, "第一帧没图标只能算未知")
            check(second["topR"] is False, "连续两帧没图标才判弃牌")
            check(first["lowL"] is True, "看到图标直接为 True")
        with patch.object(ss, "read_card_icons",
                          lambda _img, _p: {"topR": True}):
            back = s._read_icons(img)
            check(back["topR"] is True, "图标回来后立即恢复在池")
        shown_seats = s._shown_seats(((), (), (("topR", ("Ah", "Kd")),)))
        check(shown_seats == {"topR"}, f"应解析出亮牌座位：{shown_seats}")


def test_show_after_fold_keeps_fold() -> None:
    """河底才亮牌的人：之前已经判过弃牌，show 牌不能把它洗回「在池」。"""
    with stub_env():
        s = new_stream(detect_fold=True)
        s.stream.frames.extend([make_frame(), make_frame(), make_frame()])
        img = np.zeros((4, 4, 3), np.uint8)
        fold_avatar = {"lowL": ("弃牌", 0.95)}
        with patch.object(ss, "read_card_icons",
                          lambda _img, _p: {"lowL": False}), \
                patch.object(ss, "read_avatars",
                             lambda *a, **k: dict(fold_avatar)):
            s.ingest(img, "f1", 0.0, extra_signature=((), ()))
            out2 = s.ingest(img, "f2", 0.2, extra_signature=((), ()))
        check(out2.state.player("lowL").fold is True,
              f"连续两帧没图标应判弃牌：{out2.state.player('lowL').to_debug()}")
        with patch.object(ss, "read_card_icons",
                          lambda _img, _p: {"lowL": None}):
            out3 = s.ingest(img, "f3", 0.4,
                            extra_signature=((), (), (("lowL", ("Ah", "Kd")),)))
        check(out3.state.player("lowL").fold is True,
              f"亮牌不该洗掉之前的弃牌：{out3.state.player('lowL').to_debug()}")


def test_waiting_badge_needs_two_frames() -> None:
    """头像「等待中」要连续 2 帧才认：单帧误读不能把人从在局里踢出去。

    rec 对这三个字的读数很不稳（`等待中` → `练待中`/#待中，0.50–0.58），
    而名单锁建立时只要漏掉一个人，他这一手就再也不被识别（不在 roster 里
    的座位本手不参与）。所以先当「没读到」，稳住再认。
    """
    with stub_env():
        s = new_stream()
        rows = {k: dict(v) for k, v in BASE.items()}
        rows["lowR"] = {"name": "越call越幸", "chip": "200BB", "bet": ""}
        s.stream.frames.extend([make_frame(rows) for _ in range(3)])
        img = np.zeros((4, 4, 3), np.uint8)
        with patch.object(ss, "read_avatars",
                          lambda *a, **k: {"lowR": ("练待中", 0.54)}):
            out1 = s.ingest(img, "f1", 0.0)
            out2 = s.ingest(img, "f2", 0.2)
        check(out1.state.player("lowR").in_hand,
              f"第 1 帧还不该判未入局：{out1.state.player('lowR').to_debug()}")
        lowr = out2.state.player("lowR")
        check(not lowr.in_hand and lowr.status == "sitting_out",
              f"连续 2 帧后才判未入局：{lowr.to_debug()}")
        check(lowr.stack == 200.0, f"筹码要照常保留：{lowr.stack!r}")
        check(out2.state.players_in_hand == 8,
              f"未入局的人不算人数，实际 {out2.state.players_in_hand}")


def test_roster_lock_waits_for_waiting_badge() -> None:
    """有座位读到「等待中」但还没确认时，先别锁 roster。

    锁早了会把一个真没发到牌的人写进名单，而名单里的座位不再受头像文案
    影响 —— 他这一手都会被当成在局玩家（人数/位置/行动线全错）。
    """
    with stub_env():
        s = new_stream()
        rows = {k: dict(v) for k, v in BASE.items()}
        rows["lowR"] = {"name": "越call越幸", "chip": "200BB", "bet": ""}
        feed(s, make_frame(rows), extra_signature=(("As", "Kd"), ()))
        check(len(s.hand_roster) == 9,
              f"第一手应锁 9 人：{sorted(s.hand_roster)}")

        # 换手：lowR 头像「等待中」。第 1 帧只读到、还没确认。
        s.stream.frames.extend([make_frame(rows) for _ in range(2)])
        img = np.zeros((4, 4, 3), np.uint8)
        with patch.object(ss, "read_avatars",
                          lambda *a, **k: {"lowR": ("等待中", 0.9)}):
            first = s.ingest(img, "n1", 1.0,
                             extra_signature=(("Qs", "Jd"), ()))
            check(first.new_hand, "换手应被识别")
            check(s.hand_roster == {},
                  f"未确认的等待文案不该锁名单：{sorted(s.hand_roster)}")
            second = s.ingest(img, "n2", 1.2,
                              extra_signature=(("Qs", "Jd"), ()))
        check(s.hand_roster and "lowR" not in s.hand_roster,
              f"确认后锁名单应排除他：{sorted(s.hand_roster)}")
        check(not second.state.player("lowR").in_hand,
              f"确认后应判未入局：{second.state.player('lowR').to_debug()}")


def test_roster_lock_defers_at_most_three_frames() -> None:
    """文案在「等待中/正在带入」之间抖时，最多推迟 3 帧就照常锁名单。

    无限推迟会让整手都没有 roster：位置会随在场人数重排，行动顺序和 AI 上下文
    跟着错。宁可锁错一个人，也不能让位置整手都在飘。
    """
    with stub_env():
        s = new_stream()
        rows = {k: dict(v) for k, v in BASE.items()}
        rows["lowR"] = {"name": "越call越幸", "chip": "200BB", "bet": ""}
        feed(s, make_frame(rows), extra_signature=(("As", "Kd"), ()))
        s.stream.frames.extend([make_frame(rows) for _ in range(5)])
        img = np.zeros((4, 4, 3), np.uint8)
        seq = iter([("等待中", 0.9), ("正在带入", 0.9)] * 3)
        with patch.object(ss, "read_avatars",
                          lambda *a, **k: {"lowR": next(seq)}):
            for i in range(5):
                s.ingest(img, f"n{i}", 1.0 + 0.2 * i,
                         extra_signature=(("Qs", "Jd"), ()))
        check(s._roster_locked, "抖动的文案不能无限推迟名单锁")
        check("lowR" not in s.hand_roster,
              f"锁名单时仍应排除未入局的人：{sorted(s.hand_roster)}")


def test_discard_breaks_consecutive_stability() -> None:
    """动画里被质量闸丢掉的帧要打断「连续三帧」，不能跨过去凑数。"""
    with stub_env():
        s = new_stream(stable_frames=3, stable_reset_on_discard=True)
        feed(s, make_frame())
        changed = make_frame(pot="40BB")
        check(feed(s, changed, t=0.5).kind == "no_change", "第 1 帧不记录")
        check(feed(s, make_frame(pot=""), t=1.0).kind == "discard", "坏帧应丢弃")
        check(feed(s, changed, t=1.5).kind == "no_change",
              "丢弃后重新数，第 1 帧不记录")
        check(feed(s, changed, t=2.0).kind == "no_change",
              "丢弃后第 2 帧不记录")
        out = feed(s, changed, t=2.5)
        check(out.kind == "record",
              f"重新连续 3 帧后才记录，实际 {out.kind}")

    with stub_env():
        s = new_stream(stable_frames=3, stable_reset_on_discard=False)
        feed(s, make_frame())
        changed = make_frame(pot="40BB")
        feed(s, changed, t=0.5)
        feed(s, make_frame(pot=""), t=1.0)
        feed(s, changed, t=1.5)
        out = feed(s, changed, t=2.0)
        check(out.kind == "record",
              f"关闭重置时丢弃帧不打断计数，实际 {out.kind}")


def test_stable_seconds_floor() -> None:
    """帧率变快时，光数 2 帧不够，还要满足最小稳定时长。"""
    with stub_env():
        s = new_stream(stable_frames=2, stable_seconds=1.0)
        feed(s, make_frame())
        changed = make_frame(pot="40BB")
        check(feed(s, changed, t=0.5).kind == "no_change", "第 1 帧不记录")
        check(feed(s, changed, t=1.0).kind == "no_change",
              "帧数够了但时长不够，不能记录")
        out = feed(s, changed, t=1.5)
        check(out.kind == "record",
              f"稳定满 1 秒后应记录，实际 {out.kind}")


def test_roster_name_repaired_after_correction() -> None:
    """名单锁定后名字才被纠正：roster 要跟上，不能带着错拼法过完整手。

    2026-11-01 实盘：换手第一帧把 `svsvbmbm` 读成 `ssvhmbm` 并锁进 roster，
    之后 10 分钟的正确读数都被 roster 压住，整手牌/画像键全是错的。
    """
    with stub_env():
        s = new_stream(max_vanish=3, name_confirm=3)
        sig = (("Kd", "Qd"), ())
        rows = {k: dict(v) for k, v in BASE.items()}
        rows["lowL"]["name"] = "ssvhmbm"          # 锁名单时是错的
        feed(s, make_frame(rows), t=0.0, extra_signature=sig)
        check(s.hand_roster.get("lowL", (None,))[0] == "ssvhmbm",
              f"名单先按当时的读数锁定：{s.hand_roster.get('lowL')}")

        # 正确读数回来（连续 3 帧一致）
        rows["lowL"]["name"] = "svsvbmbm"
        out = None
        for i in range(3):
            out = feed(s, make_frame(rows), t=0.5 * (i + 1),
                       extra_signature=sig)
        check(s.hand_roster["lowL"][0] == "svsvbmbm",
              f"确认之后名单应被修正：{s.hand_roster['lowL']}")
        check(out.state.player("lowL").name == "svsvbmbm",
              f"本帧输出也应用新名字：{out.state.player('lowL').name}")
        check(any("名单名字已修正" in w for w in out.state.name_warnings),
              f"修正要能看见：{out.state.name_warnings}")
        check(s.summary()["name_fixes"] >= 1, "summary 应统计修正次数")


def test_roster_name_not_repaired_before_confirmation() -> None:
    """只读到一帧的新名字（还没确认）不能改名单 —— 名单是这一手的身份基准。"""
    with stub_env():
        s = new_stream(max_vanish=3, name_confirm=3)
        sig = (("Kd", "Qd"), ())
        rows = {k: dict(v) for k, v in BASE.items()}
        rows["lowL"]["name"] = "ssvhmbm"
        feed(s, make_frame(rows), t=0.0, extra_signature=sig)

        rows["lowL"]["name"] = "svsvbmbm"
        out = feed(s, make_frame(rows), t=0.5, extra_signature=sig)
        check(s.hand_roster["lowL"][0] == "ssvhmbm",
              f"没确认前不能改名单：{s.hand_roster['lowL']}")
        check(out.state.player("lowL").name == "ssvhmbm",
              f"输出仍按名单：{out.state.player('lowL').name}")


def test_lexicon_corrects_known_player_name() -> None:
    """词表命中已知玩家：读数直接按已知拼写记录，并留下说明。"""
    from poker_ocr.name_lexicon import NameLexicon

    with stub_env():
        lexicon = NameLexicon(["台风来咯咯", "爽歪的麻雀"])
        s = new_stream(max_vanish=3, name_lexicon=lexicon)
        rows = {k: dict(v) for k, v in BASE.items()}
        rows["midL"]["name"] = "台风来吧多"       # 尾巴被读坏
        out = feed(s, make_frame(rows), t=0.0, extra_signature=(("Kd",), ()))
        check(out.state.player("midL").name == "台风来咯咯",
              f"应纠正到已知拼法：{out.state.player('midL').name}")
        check(out.state.player("midL").name_raw == "台风来吧多",
              f"原始读数要保留下来：{out.state.player('midL').name_raw}")

def test_lexicon_flags_suspect_without_changing() -> None:
    """存疑档（像但不够把把握）：不改名字，只写告警。"""
    from poker_ocr.name_lexicon import NameLexicon

    with stub_env():
        lexicon = NameLexicon(["svsvbmbm"])
        s = new_stream(max_vanish=3, name_lexicon=lexicon)
        rows = {k: dict(v) for k, v in BASE.items()}
        rows["upL"]["name"] = "ssvhmbm"          # 0.667，公共前缀只有 1
        out = feed(s, make_frame(rows), t=0.0, extra_signature=(("Kd",), ()))
        check(out.state.player("upL").name == "ssvhmbm",
              f"存疑档不该自动改：{out.state.player('upL').name}")
        check(any("疑似已知玩家" in w for w in out.state.name_warnings),
              f"存疑读数要告警：{out.state.name_warnings}")
        check(not out.state.errors, "名字告警不能进质量闸")


def test_lexicon_never_merges_two_live_players() -> None:
    """桌上同时有「俊伯老公」和「俊伯老婆」时，不能把后者并进前者。"""
    from poker_ocr.name_lexicon import NameLexicon

    with stub_env():
        lexicon = NameLexicon(["俊伯老公"])
        s = new_stream(max_vanish=3, name_lexicon=lexicon)
        rows = {k: dict(v) for k, v in BASE.items()}
        rows["midL"]["name"] = "俊伯老公"
        rows["lowL"]["name"] = "俊伯老婆"
        out = None
        for i in range(3):
            out = feed(s, make_frame(rows), t=0.5 * i,
                       extra_signature=(("Kd",), ()))
        check(out.state.player("lowL").name == "俊伯老婆",
              f"另一个座位已经在用「俊伯老公」，不能并名："
              f"{out.state.player('lowL').name}")
        check(out.state.player("midL").name == "俊伯老公",
              f"原来的那位不应改名：{out.state.player('midL').name}")


def main() -> int:
    tests = [
        test_first_frame_is_baseline,
        test_same_signature_is_not_recorded_again,
        test_change_needs_two_stable_frames,
        test_single_stable_frame_records_immediately,
        test_discard_does_not_advance_prev,
        test_discard_breaks_consecutive_stability,
        test_stable_seconds_floor,
        test_vanished_seat_needs_max_vanish_frames,
        test_departed_seat_with_chip_garbage_recovers,
        test_exit_mid_hand_keeps_name_position_and_marks_fold,
        test_new_player_mid_hand_kept_out_until_next_hand,
        test_occluded_player_is_not_marked_left,
        test_plus_placeholder_marks_left_and_icon_return_revives,
        test_lineup_change_between_hands_does_not_discard,
        test_dead_button_dealer_left_keeps_recording,
        test_initial_blinds_carried_when_banner_unreadable,
        test_reset_makes_next_frame_a_new_baseline,
        test_dealer_rotation_clears_all_in_carry,
        test_all_in_badge_disappearance_ends_hand,
        test_all_in_badge_disappears_during_hero_turn_is_not_hand_end,
        test_all_in_badge_end_waits_for_river,
        test_hand_boundaries_from_cards_and_board,
        test_slow_fields_pinned_until_hand_switch,
        test_name_pin_expires_so_action_badges_can_be_read,
        test_bomb_banner_latches_across_frames,
        test_card_icon_fold_needs_two_absent_frames,
        test_waiting_badge_needs_two_frames,
        test_roster_lock_waits_for_waiting_badge,
        test_roster_lock_defers_at_most_three_frames,
        test_show_after_fold_keeps_fold,
        test_roster_name_repaired_after_correction,
        test_roster_name_not_repaired_before_confirmation,
        test_lexicon_corrects_known_player_name,
        test_lexicon_flags_suspect_without_changing,
        test_lexicon_never_merges_two_live_players,
    ]
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
    print("\n✅ 状态机全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
