#!/usr/bin/env python3
"""8 人桌 profile（config/table_wpk_mac_8p.json）的几何守卫。

2026-11-16 从「8 人桌.mov」（1472x1676）实测出来：顶排从 9 人桌的两座变成**一座**。
这份配置的其余 7 个座位是从 9 人桌逐字节照抄的，唯一新量的是 top 座——所以这里
只钉最容易在后续维护里被改坏的性质，不重复测已经由 9 人桌覆盖的东西：

1. 8 座、ring_index 0..7 各一个、ring_order 与 ring_index 一致、hero 在环里；
2. check_ring_geometry 通过（Profile.load 已经跑过一次，这里显式再跑，报错更直白）；
3. top 是**唯一**的顶排座，名字槽横跨画面中线（实测名字中心 x≈0.5003）；
4. chip 槽与 bet 槽不重叠（实测只隔 4.3px），bet 槽不碰底池横幅；
5. action_slot 就是 name ∪ avatar（9 人桌的约定：动作徽章画在名字位、弃牌画在头像）。

    nv tests/test_profile_8p.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.profile import Profile   # noqa: E402

PROFILE = ROOT / "config" / "table_wpk_mac_8p.json"
FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def _center(slot) -> tuple[float, float]:
    x1, y1, x2, y2 = slot
    return (x1 + x2) / 2, (y1 + y2) / 2


def _union(a, b) -> list[float]:
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def test_profile_shape() -> None:
    p = Profile.load(str(PROFILE))
    ids = [s.seat_id for s in p.seats]

    check(len(p.seats) == 8, f"应当 8 座，实际 {len(p.seats)} 座：{ids}")
    check(sorted(s.ring_index for s in p.seats) == list(range(8)),
          f"ring_index 必须是 0..7 各一个，实际 {sorted(s.ring_index for s in p.seats)}")
    check(len(set(ids)) == len(ids), f"seat_id 有重复：{ids}")

    by_ring = [s.seat_id for s in sorted(p.seats, key=lambda s: s.ring_index)]
    check(by_ring == list(p.ring_order),
          f"ring_order 与 ring_index 不一致：{p.ring_order} vs {by_ring}")

    check(p.hero_seat_id in ids, f"hero_seat_id={p.hero_seat_id} 不在 seats 里")
    check(p.hero_seat_id == "hero", f"hero 应当叫 hero，实际 {p.hero_seat_id}")
    check(p.action_direction == "cw", f"action_direction 实测 cw，实际 {p.action_direction}")
    check(p.direction_verified is True, "cw 已由帧 4000/9000 的盲注位置实测，direction_verified 应为 true")
    check(p.table_center == (0.5, 0.43), f"table_center 应继承 9 人桌 (0.5, 0.43)，实际 {p.table_center}")
    check(p.reference_size == (1472, 1676), f"reference_size 应为 1472x1676，实际 {p.reference_size}")
    check("top" in ids, "8 人桌必须有一个名为 top 的顶排座")
    check("topL" not in ids and "topR" not in ids,
          "8 人桌只剩一个顶排座；留着 topL/topR 会让 check_ring_geometry 报「环序对不上」")


def test_ring_geometry() -> None:
    p = Profile.load(str(PROFILE))
    errs = p.check_ring_geometry()
    check(not errs, f"check_ring_geometry 失败：{errs}")


def test_top_seat_geometry() -> None:
    p = Profile.load(str(PROFILE))
    top = p.seat("top")

    for name, slot in (("name", top.name_slot), ("chip", top.chip_slot),
                       ("bet", top.bet_slot), ("avatar", top.avatar_slot),
                       ("action", top.action_slot)):
        x1, y1, x2, y2 = slot
        check(0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0,
              f"top.{name}_slot 不是合法的归一化框：{slot}")

    # 顶排单座：名字中心必须在中线上（实测 0.5003），左右各 3 座都不跨中线
    cx, cy = _center(top.name_slot)
    check(abs(cx - 0.5) <= 0.01, f"top 名字中心应贴着中线 x≈0.5，实际 {cx:.4f}")
    check(top.name_slot[0] <= 0.5 <= top.name_slot[2], f"top 名字槽应当横跨画面中线：{top.name_slot}")
    check(0.19 <= cy <= 0.215, f"top 名字中心 y 实测 ≈0.204，实际 {cy:.4f}")

    # 名字槽要容得下长名字：实测 6 个汉字 105px（0.0713），槽 171px（0.1160）
    check(top.name_slot[2] - top.name_slot[0] >= 0.10,
          f"top 名字槽太窄，长名字会被裁：{top.name_slot[2] - top.name_slot[0]:.4f}")

    # chip 与 bet 上下紧贴（实测只隔 4.3px），谁都不许压到对方
    check(top.chip_slot[3] <= top.bet_slot[1],
          f"top.chip_slot 底边 {top.chip_slot[3]} 压进了 bet_slot 顶边 {top.bet_slot[1]}")

    # bet 槽上沿不能太高（否则吃进筹码行），下沿不能碰到本街总额/底池横幅
    check(0.29 <= top.bet_slot[1] <= 0.31, f"top.bet_slot 顶边应 ≈0.299，实际 {top.bet_slot[1]}")
    pot = p.global_slots.get("pot")
    check(pot is not None, "global_slots 缺 pot 槽位")
    if pot is not None:
        check(top.bet_slot[3] <= pot[1],
              f"top.bet_slot 底边 {top.bet_slot[3]} 碰到/越过底池横幅顶边 {pot[1]}")

    # action = name ∪ avatar（动作徽章画在名字位、弃牌画在头像上）
    expect = _union(top.name_slot, top.avatar_slot)
    check(all(abs(a - b) <= 1e-6 for a, b in zip(top.action_slot, expect)),
          f"top.action_slot 应等于 name ∪ avatar：{top.action_slot} vs {expect}")

    check(top.chip_slot_alt is None, "top 没有第二个筹码槽位；8 人桌的 D 只漂一小段")


def test_side_seats_copied() -> None:
    """侧座 / hero 是照抄 9 人桌的：槽位必须与 9 人桌逐值相同（防止有人只改一边）。"""
    p8 = Profile.load(str(PROFILE))
    p9 = Profile.load(str(ROOT / "config" / "table_wpk_mac.json"))
    for sid in ("hero", "lowL", "midL", "upL", "upR", "midR", "lowR"):
        a, b = p8.seat(sid), p9.seat(sid)
        for field in ("name_slot", "chip_slot", "avatar_slot", "bet_slot", "action_slot"):
            check(getattr(a, field) == getattr(b, field),
                  f"{sid}.{field} 应当与 9 人桌一致：8p={getattr(a, field)} vs 9p={getattr(b, field)}")


def test_preflop_min_open_floor() -> None:
    """开池下限是 profile 字段（#112）：无前注 8 人桌取缺省 5.5，越界报 2-20。"""
    import dataclasses

    from poker_ocr.profile import DEFAULT_PREFLOP_MIN_OPEN_BB

    p = Profile.load(str(PROFILE))
    check(p.default_ante is None,
          f"8 人桌没有前注，不该兜底：{p.default_ante}")
    check(DEFAULT_PREFLOP_MIN_OPEN_BB == 5.5,
          f"缺省开池下限应是 5.5：{DEFAULT_PREFLOP_MIN_OPEN_BB}")
    check(p.preflop_min_open_bb == DEFAULT_PREFLOP_MIN_OPEN_BB,
          f"无前注桌应取缺省下限：{p.preflop_min_open_bb}")
    problems = dataclasses.replace(p, preflop_min_open_bb=21.0).check()
    check(any("preflop_min_open_bb" in q and "2-20" in q for q in problems),
          f"越界下限应报 2-20：{problems}")


def main() -> int:
    tests = [test_profile_shape, test_ring_geometry, test_top_seat_geometry,
             test_side_seats_copied, test_preflop_min_open_floor]
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
    print("\n✅ 8 人桌 profile 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
