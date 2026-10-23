"""从庄位推出每个座位的位置名称（SB / BB / UTG / ... / BTN）。

这份表和 call-back 项目 `utils/hand.go` 的 `positionsByTableSize` 是同一份契约，
改动要两边同步。契约的要点：**每个位置列表都是按翻前行动顺序排的**
（SB 先说话，BTN 最后），所以从庄位左边起沿行动方向走，正好逐个对上。

    庄家左边第一个 = SB，第二个 = BB，... 绕回来最后一个 = BTN（庄家自己）

**庄家不在场（死按钮）**：牌局中途有人退出 / 暂离时，庄位按钮可能还留在
那个人身上，这一手剩下的玩家照常从按钮下家开始行动。此时不能因为「庄位
不在在场环上」丢掉整帧 —— 传 `seat_ring`（整张桌的固定座位环，含空座），
位置就按按钮在完整环上的位置来排，空出来的座位只是被跳过。按钮仍然占一个
座位：这一手按 n+1 人桌发牌，用 n+1 人的位置表去掉末尾的 BTN，剩下的
SB..CO 与每个人实际的相对位置一致（场上没有真正的 BTN 玩家）。
"""

from __future__ import annotations

from dataclasses import dataclass

SB = "SB"
BB = "BB"
UTG = "UTG"
UTG1 = "UTG+1"
UTG2 = "UTG+2"
LJ = "LJ"
HJ = "HJ"
CO = "CO"
BTN = "BTN"

MIN_TABLE_SIZE = 2
MAX_TABLE_SIZE = 9

# 各人数下的合法位置，按翻前行动顺序。与 call-back/utils/hand.go 逐项对应。
# 规律：9 人桌去掉 UTG+2 就是 8 人，再去掉 UTG+1 就是 7 人，以此类推。
POSITIONS_BY_TABLE_SIZE: dict[int, list[str]] = {
    9: [SB, BB, UTG, UTG1, UTG2, LJ, HJ, CO, BTN],
    8: [SB, BB, UTG, UTG1, LJ, HJ, CO, BTN],
    7: [SB, BB, UTG, LJ, HJ, CO, BTN],
    6: [SB, BB, UTG, HJ, CO, BTN],
    5: [SB, BB, UTG, CO, BTN],
    4: [SB, BB, UTG, BTN],
    3: [SB, BB, BTN],
    2: [SB, BB],
}


@dataclass
class PositionAssignment:
    positions: dict[str, str]        # seat_id -> 位置名
    table_size: int
    dealer_seat_id: str | None
    ok: bool
    error: str | None = None
    # 庄家不在场（退出 / 暂离），按死按钮排的位置。ok 仍为 True：位置是
    # 可用的，只是没有真正的 BTN 玩家。
    dead_button: bool = False


def assign_positions(ring_seat_ids: list[str], dealer_seat_id: str | None,
                     *, seat_ring: list[str] | None = None) -> PositionAssignment:
    """按庄位给环上的座位定位置。

    ring_seat_ids 必须是**按行动方向排好的、只含有人座位的**环序。
    人数就是环长 —— 空座位不发牌、不占位置。

    `seat_ring` 是整张桌的固定座位环（含当前空座），顺序与 `ring_seat_ids`
    相同。传了它就沿这张固定环走、只给在场的人发位置：空座只跳过，按钮
    始终是环上那个固定座位 —— 庄家自己退出 / 暂离时也能按死按钮排，
    而不是判「庄位不在环上」丢掉整帧。不传时保持旧行为（按在场人数重新
    编号，庄家必须在场）。
    """
    if not dealer_seat_id:
        return PositionAssignment({}, len(ring_seat_ids), None, ok=False,
                                  error="没有庄位，无法定位置")

    n = len(ring_seat_ids)
    if not (MIN_TABLE_SIZE <= n <= MAX_TABLE_SIZE):
        return PositionAssignment({}, n, dealer_seat_id, ok=False,
                                  error=f"{n} 人桌不在支持范围 "
                                        f"{MIN_TABLE_SIZE}-{MAX_TABLE_SIZE}")

    table = POSITIONS_BY_TABLE_SIZE[n]

    if seat_ring is not None:
        # ── 按固定座位环走：空座只跳过，不重新编号 ──────────────────
        if dealer_seat_id not in seat_ring:
            return PositionAssignment({}, n, dealer_seat_id, ok=False,
                                      error=f"庄位 {dealer_seat_id} 不在环上")
        active = set(ring_seat_ids)
        missing = active - set(seat_ring)
        if missing:
            return PositionAssignment({}, n, dealer_seat_id, ok=False,
                                      error=f"座位 {sorted(missing)} 不在完整座位环上")
        dealer_idx = seat_ring.index(dealer_seat_id)
        walk = seat_ring[dealer_idx:] + seat_ring[:dealer_idx]
        walk = [sid for sid in walk if sid in active]
        dead_button = dealer_seat_id not in active
        if n == 2:
            # 单挑：按钮位下小盲。庄家在场时 walk[0] 是庄家；死按钮时
            # walk[0] 是按钮下家 —— 两种情况下第一个在场的人都拿 SB。
            positions = {walk[0]: SB, walk[1]: BB}
        elif dead_button:
            # 按钮上没有玩家，但它仍然占一个座位：这一手是按 n+1 人桌发的牌。
            # 用 n+1 人的位置表去掉末尾的 BTN，剩下的 SB..CO 才和每个人
            # 实际的相对位置一致（不能按 n 人桌重新编号，否则中间位置会整体
            # 往前挪一位）。
            effective = min(n + 1, MAX_TABLE_SIZE)
            labels = POSITIONS_BY_TABLE_SIZE[effective][:n]
            positions = {seat: labels[j] for j, seat in enumerate(walk)}
        else:
            # 庄家拿 BTN，他下家拿 SB，其余按行动顺序排。
            positions = {seat: table[(j - 1) % n] for j, seat in enumerate(walk)}
        return PositionAssignment(positions, n, dealer_seat_id, ok=True,
                                  dead_button=dead_button)

    if dealer_seat_id not in ring_seat_ids:
        return PositionAssignment({}, n, dealer_seat_id, ok=False,
                                  error=f"庄位 {dealer_seat_id} 不在环上")

    dealer_idx = ring_seat_ids.index(dealer_seat_id)

    # 从庄家开始，沿行动方向走一圈
    walk = ring_seat_ids[dealer_idx:] + ring_seat_ids[:dealer_idx]

    if n == 2:
        # 单挑是唯一的例外：按钮位下小盲，庄家自己就是 SB。
        # 通用公式会把庄家算成 BB，所以这里单独处理。
        # 位置名仍写 SB（契约里 2 人桌只有 SB/BB 两个合法值），
        # 「他是庄家」这件事由 is_dealer 字段表达。
        positions = {walk[0]: SB, walk[1]: BB}
        return PositionAssignment(positions, n, dealer_seat_id, ok=True)

    # 通用情形：庄家拿列表最后一个（BTN），他左边第一个拿列表第一个（SB）。
    # 也就是偏移 j 位的人拿 table[(j - 1) % n]。
    positions = {seat: table[(j - 1) % n] for j, seat in enumerate(walk)}
    return PositionAssignment(positions, n, dealer_seat_id, ok=True)


def positions_for_table_size(table_size: int) -> list[str]:
    return POSITIONS_BY_TABLE_SIZE.get(table_size, [])
