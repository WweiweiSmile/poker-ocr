#!/usr/bin/env python3
"""ROI 去重的单元测试（M2.2）：合成图 + 假 OCR 引擎，不依赖模型和照片。

要锁住的核心是**一个危险的失败模式**：画面其实变了、判据说没变 → 复用过期读数，
而它在下游看起来和「识别器读对了」一模一样。所以这里的测试分三组：

  1. **判据本身**：粗网格（9x8）会把「只改一个字符的长文本」判成没变 ——
     这是实测踩到的真坑（真实帧上 73 次复用里有 2 次过期读数），必须留个回归。
     细网格（17x16）和 MAD 各自要能看见这种变化。
  2. **缓存行为**：没变就不调 rec（用假引擎的调用次数数）、变了就重跑、
     复用的读数是**副本**（不能就地改缓存里那份）。
  3. **安全阀**：分辨率/参数一变，缓存必须整份作废，不能跨场景复用。

不依赖 pytest，直接跑：
    .venv/bin/python tests/test_roi_dedup.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from poker_ocr.profile import Profile  # noqa: E402
from poker_ocr.roi_dedup import (  # noqa: E402
    RoiDedupCache, RoiStream, dhash, dhash_tiles, hash_distance, hamming,
    pixel_mad,
)
from poker_ocr.roi_reader import RoiRead  # noqa: E402
from test_roi_reader import (  # noqa: E402
    DISC, FELT, SIZE, StubEngine, build_table, make_marked,
)

WPK = ROOT / "config" / "table_wpk_1080x2400.json"

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def long_text_crop(text: str) -> np.ndarray:
    """模拟盲注那条 475px 宽的长文本 ROI（房间号 + 计时器）。"""
    img = np.full((50, 475, 3), FELT, dtype=np.uint8)
    cv2.putText(img, text, (6, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                (255, 255, 255), 2, cv2.LINE_AA)
    return img


def hero_name_rect(profile: Profile) -> tuple[int, int, int, int]:
    return profile.slot_to_px(profile.seat("hero").name_slot, SIZE, pad=0.05)


# ─────────────────────────── 1. 判据 ───────────────────────────

def test_one_changed_digit_in_long_text() -> None:
    """长文本里只改一个字符 —— 这是去重最容易骗过自己的形状，**分块就是为它加的**。

    实测：475px 宽的盲注 ROI 只改一个字符时，**整块 dhash 的距离是 0**
    （那个字符只占全宽 4%，什么网格都平均掉了），于是复用了过期读数。
    横切 4 片后同一个变化给出 3 bit。这个测试就是那个反例的回归 ——
    如果有人把 TILES 改回 1，它会立刻红。
    """
    a = long_text_crop("52122200:40:18")
    b = long_text_crop("52122200:40:13")

    whole = hamming(dhash(a), dhash(b))
    tiled4 = hash_distance(dhash_tiles(a, 4), dhash_tiles(b, 4))
    tiled8 = hash_distance(dhash_tiles(a, 8), dhash_tiles(b, 8))

    check(whole == 0, f"整块 hash 本该看不见这个变化（实测 {whole}）——"
                      f"若这里变了，说明 hash 实现被改过，分块的依据要重新量")
    check(tiled4 > 0, f"切 4 片必须能看见这个变化，实际 {tiled4}")
    check(tiled8 > tiled4, f"切得越细越敏感：8 片（{tiled8}）应大于 4 片（{tiled4}）")


def test_tiles_are_what_makes_the_default_safe() -> None:
    """默认配置必须能扛住上面的反例 —— 这是「默认 TILES=4」的全部理由。"""
    from poker_ocr.roi_dedup import TILES
    a, b = long_text_crop("52122200:40:18"), long_text_crop("52122200:40:13")
    check(TILES > 1, f"默认 TILES 必须大于 1，实际 {TILES}")
    check(hash_distance(dhash_tiles(a, TILES), dhash_tiles(b, TILES)) > 0,
          f"默认 {TILES} 片必须能看见这个变化 —— 否则会复用过期读数")


def test_identical_crops_hash_equal() -> None:
    a = long_text_crop("52122200:40:18")
    check(hamming(dhash(a), dhash(a.copy())) == 0, "同一张图两次 hash 应完全相同")
    check(hash_distance(dhash_tiles(a), dhash_tiles(a.copy())) == 0,
          "同一张图两次分块 hash 应完全相同")
    check(pixel_mad(cv2.cvtColor(a, cv2.COLOR_BGR2GRAY),
                    cv2.cvtColor(a.copy(), cv2.COLOR_BGR2GRAY)) == 0.0, "MAD 应为 0")


def test_narrow_roi_is_not_tiled() -> None:
    """太窄的 ROI 不切（否则每片只剩几个像素，hash 变成噪声）。"""
    narrow = np.full((30, 20, 3), FELT, dtype=np.uint8)
    check(len(dhash_tiles(narrow, 4)) == 1,
          f"20px 宽的 ROI 不该切成 4 片，实际 {len(dhash_tiles(narrow, 4))} 片")
    wide = long_text_crop("52122200:40:18")
    check(len(dhash_tiles(wide, 4)) == 4, "475px 宽应该切得动 4 片")


def test_pixel_mad_basics() -> None:
    a = np.zeros((4, 4), dtype=np.uint8)
    check(pixel_mad(a, a.copy()) == 0.0, "相同 → 0")
    check(pixel_mad(a, a + 1) == 1.0, f"整体 +1 → 1.0，实际 {pixel_mad(a, a + 1)}")
    check(pixel_mad(a, np.zeros((5, 4), dtype=np.uint8)) == float("inf"),
          "尺寸不同要报 inf（宁可当成变了）")


def test_hash_grid_must_fill_whole_bytes() -> None:
    """(w-1)*h 不是 8 的倍数就没法整字节打包 —— 那种网格要直接报错，不能凑合。"""
    try:
        dhash(np.zeros((10, 10, 3), dtype=np.uint8), 5, 5)
    except ValueError:
        return
    FAILS.append("(5-1)*5=20 不是 8 的倍数，dhash 应该报错")


# ─────────────────────────── 2. 缓存行为 ───────────────────────────

def test_unchanged_frame_runs_no_rec_at_all() -> None:
    """同一张图连读两帧：第二帧一次 rec 都不该跑，读数全部复用。"""
    profile = Profile.load(WPK)
    img = make_marked()
    engine = StubEngine(build_table(profile))
    stream = RoiStream(profile, engine, with_dealer=False)
    stream.read(img, frame_id="f1")
    calls_after_first = len(engine.crops)

    frame2 = stream.read(img, frame_id="f2")
    check(len(engine.crops) == calls_after_first,
          f"第二帧一模一样，不该再跑 rec（多了 {len(engine.crops) - calls_after_first} 次）")
    check(frame2.n_reused == frame2.n_roi,
          f"第二帧应全部复用，实际 {frame2.n_reused}/{frame2.n_roi}")
    check(all(r.reused for s in frame2.seats for r in s.fields.values()),
          "复用标记没打上")
    check(frame2.text("name", "hero") == "hero:name",
          f"复用回来的文本应还是 hero:name，实际 {frame2.text('name', 'hero')!r}")
    # hit_rate 的分母含第一帧那 30 次 miss，所以这里看的是「第二帧整帧命中」
    check(stream.cache.hits == frame2.n_roi,
          f"第二帧应产生 {frame2.n_roi} 次命中，实际 {stream.cache.hits}")


def test_changed_roi_reruns_and_neighbours_reuse() -> None:
    """只改一块 ROI 的画面：那块要重跑 rec，别块继续复用。"""
    profile = Profile.load(WPK)
    img = make_marked()
    table = build_table(profile)
    engine = StubEngine(table)
    stream = RoiStream(profile, engine, with_dealer=False)
    stream.read(img, frame_id="f1")

    # 在 hero 的名字槽里涂一块（刻意避开裁剪图的第一个像素 ——
    # 假引擎靠它反解裁剪坐标，动了它整张表的查表就失效了）
    x1, y1, x2, y2 = hero_name_rect(profile)
    changed = make_marked()
    changed[y1 + 1:y1 + 4, x1 + 20:x1 + 120] = DISC

    frame2 = stream.read(changed, frame_id="f2")
    hero = frame2.seat("hero").fields["name"]
    lowl = frame2.seat("lowL").fields["name"]
    check(not hero.reused, "hero 的名字被涂了，必须重跑 rec")
    check(lowl.reused, "lowL 没变，应该复用")
    check(frame2.n_reused == frame2.n_roi - 1,
          f"应只重跑 1 块，实际重跑 {frame2.n_roi - frame2.n_reused} 块")


def test_reused_read_is_a_copy() -> None:
    """复用的读数必须是副本 —— 就地改会把缓存里那份也标成 reused，
    下一帧命中时它看上去就像「当初也是复用来的」。"""
    profile = Profile.load(WPK)
    img = make_marked()
    engine = StubEngine(build_table(profile))
    stream = RoiStream(profile, engine, with_dealer=False)
    frame1 = stream.read(img, frame_id="f1")
    fresh = frame1.seat("hero").fields["name"]
    check(not fresh.reused, "第一帧是新读的，不该带 reused 标记")

    stream.read(img, frame_id="f2")
    check(not fresh.reused,
          "第二帧的复用把第一帧那份读数也改了 —— 说明没复制（dc_replace 漏了）")


def test_low_score_read_ignores_mad_fallback() -> None:
    """低分读数只认严格命中：像素真动了一点（MAD 本会放过）时必须重读。

    这是「前几帧读错了还能纠正回来」的唯一机会 —— MAD 是宽松判据，低分读数
    再叠一层宽松判据，一个可能是错的值就会一直挂在那儿，而下游看到它和
    「读对了」一模一样。
    """
    x = np.tile(np.arange(40, dtype=np.uint8) * 4, (40, 1))
    roi = np.dstack([x] * 3)

    def arm(cache: RoiDedupCache, read: RoiRead) -> None:
        # 先喂一帧建立 prev hash/gray，再伪造一个不同的 hash：
        # 这样 check 一定走「dhash 没中」，只有 MAD 那道闸能救 —— 正是要考的路径
        cache.check("hero.chip", roi)
        cache.store("hero.chip", read)
        cache._hash["hero.chip"] = (12345,)      # 分块数不同 → 距离 inf

    low = RoiRead("chip", (0, 0, 40, 40), [("271O", 0.42)])
    cache = RoiDedupCache()
    arm(cache, low)
    hit, _ = cache.check("hero.chip", roi)
    check(hit is None, f"低分读数的 key 不该吃 MAD 兜底，却命中了 {hit}")
    check(cache.blocked_by_strict == 1,
          f"被拦下的次数应记 1，实际 {cache.blocked_by_strict}")

    # 对照：同一个场景，换成可信读数 —— MAD 兜底照旧生效
    confident = RoiRead("chip", (0, 0, 40, 40), [("2710BB", 0.93)])
    cache2 = RoiDedupCache()
    arm(cache2, confident)
    hit, _ = cache2.check("hero.chip", roi)
    check(hit is not None and hit.text == "2710BB",
          f"高分读数应继续吃 MAD 兜底，实际 {hit}")
    check(cache2.hits_mad == 1, f"应记一次 MAD 命中，实际 {cache2.hits_mad}")
    check(cache2.blocked_by_strict == 0, "高分读数不该被 strict 拦")


def test_empty_read_stays_cheap() -> None:
    """空读数（这块没字）不标 strict：重读一次还是读不出，纯浪费。

    实测一帧 32 块里有 15 块是空读数（空下注位、没有横幅），全标 strict 等于
    每帧多跑 15 次 rec。
    """
    x = np.tile(np.arange(40, dtype=np.uint8) * 4, (40, 1))
    roi = np.dstack([x] * 3)
    cache = RoiDedupCache()
    cache.check("@bomb_cn", roi)
    cache.store("@bomb_cn", RoiRead("bomb_cn", (0, 0, 40, 40), []))
    cache._hash["@bomb_cn"] = (12345,)
    hit, _ = cache.check("@bomb_cn", roi)
    check(hit is not None, "空读数应继续吃 MAD 兜底（复用），不该重读")
    check(cache.blocked_by_strict == 0, "空读数不该进 strict")


def test_photo_noise_is_not_treated_as_uncertain_text() -> None:
    """头像照片读出来的碎片（0.1–0.3）不标 strict：重读还是碎片，而 det+rec 最贵。

    实测一帧 9 个座位的头像分数全在 0.10–0.32（`舞` `展` `A` `G`），把这块也拉去
    每帧重读等于白花 9 次 det。
    """
    x = np.tile(np.arange(40, dtype=np.uint8) * 4, (40, 1))
    roi = np.dstack([x] * 3)
    cache = RoiDedupCache()
    cache.check("hero.@avatar", roi)
    cache.store("hero.@avatar", RoiRead("avatar", (0, 0, 40, 40), [("舞", 0.10)]))
    cache._hash["hero.@avatar"] = (12345,)
    hit, _ = cache.check("hero.@avatar", roi)
    check(hit is not None and hit.text == "舞",
          "分数低到像照片碎片的读数应继续复用（MAD 兜底），不该每次重读")
    check(cache.blocked_by_strict == 0, "照片碎片不该进 strict")


def test_low_score_read_cannot_be_pinned() -> None:
    """低分读数不许钉住：钉住 = 这一手不再重读，错读就没机会纠正了。"""
    cache = RoiDedupCache()
    cache.check("@blinds", np.zeros((40, 40, 3), np.uint8))
    cache.store("@blinds", RoiRead("@blinds", (0, 0, 40, 40), [("1/2", 0.55)]))
    check(not cache.pin("@blinds"), "低分读数不该允许 pin")
    check(not cache.is_pinned("@blinds"), "pin 失败了却进了 pinned 集合")

    # 同一块后来读到可信值 → 允许钉住
    cache.store("@blinds", RoiRead("@blinds", (0, 0, 40, 40), [("1/2", 0.96)]))
    check(cache.pin("@blinds"), "读到可信值后应能钉住")
    check("strict_keys" in cache.to_debug(), "to_debug 应暴露 strict 统计")


def test_pinned_key_ignores_pixel_changes_until_unpinned() -> None:
    """钉住的名字/盲注：画面变了也复用，换手 unpin_all 后重新按像素判断。"""
    cache = RoiDedupCache()
    # 用横/竖渐变做两张真正不同的图：纯色图 dhash 都是 0，测不出变化
    x = np.tile(np.arange(40, dtype=np.uint8) * 5, (40, 1))
    roi = np.dstack([x] * 3)
    y = np.tile((np.arange(40, dtype=np.uint8) * 5)[:, None], (1, 40))
    changed = np.dstack([y] * 3)
    read = RoiRead("name", (0, 0, 40, 40), [("Alice", 0.95)])
    cache.check("hero.name", roi)
    cache.store("hero.name", read)
    check(cache.pin("hero.name"), "有读数时应能钉住")
    check(cache.is_pinned("hero.name"), "钉住状态不对")

    hit, _ = cache.check("hero.name", changed)
    check(hit is not None and hit.text == "Alice",
          f"钉住后画面怎么变都应复用，实际 {hit}")
    check(cache.hits_pinned == 1, f"钉住命中应单独计数，实际 {cache.hits_pinned}")

    cache.unpin_all()
    hit, _ = cache.check("hero.name", changed)
    check(hit is None, "解钉后画面变了应重读（返回 None）")

    # 钉住但没有读数（缓存被清过）不能命中
    cache2 = RoiDedupCache()
    check(not cache2.pin("nobody"), "没有读数时 pin 应返回 False")


def test_unpin_single_key_restores_reread() -> None:
    """unpin(key) 只解一块：名字到期复查动作徽章，盲注仍整手钉住。

    动作徽章画在名字槽上，名字钉死整手就再也读不到让牌/下注 —— 到期解钉
    是 live 行动线唯一的实时来源（见 state_stream.NAME_PIN_REFRESH_FRAMES）。
    """
    cache = RoiDedupCache()
    x = np.tile(np.arange(40, dtype=np.uint8) * 5, (40, 1))
    roi = np.dstack([x] * 3)
    y = np.tile((np.arange(40, dtype=np.uint8) * 5)[:, None], (1, 40))
    changed = np.dstack([y] * 3)
    for key in ("hero.name", "@blinds"):
        cache.check(key, roi)
        cache.store(key, RoiRead(key, (0, 0, 40, 40), [("Alice", 0.95)]))
        check(cache.pin(key), f"{key} 有读数时应能钉住")

    cache.unpin("hero.name")
    check(not cache.is_pinned("hero.name"), "名字应已解钉")
    check(cache.is_pinned("@blinds"), "只该解名字，盲注要继续钉")

    hit, _ = cache.check("hero.name", changed)
    check(hit is None, "解钉后画面变了应重读（返回 None）")
    hit, _ = cache.check("@blinds", changed)
    check(hit is not None and hit.text == "Alice",
          f"没解钉的盲注仍应复用，实际 {hit}")


def test_mad_eps_defaults_to_0_2() -> None:
    """默认开 0.2：真机视频上精确 hash 只有 0.5% 命中，不开等于没去重。
    传 0.0 可以退回纯 dhash；统计里要能区分两条命中路径。"""
    profile = Profile.load(WPK)
    engine = StubEngine({})
    on = RoiStream(profile, engine)
    check(on.cache.mad_eps == 0.2, f"mad_eps 默认应为 0.2，实际 {on.cache.mad_eps}")
    check("by_mad" in on.cache.to_debug(), "默认开着时统计里应有 by_mad")

    off = RoiStream(profile, engine, mad_eps=0.0)
    check(off.cache.mad_eps == 0.0, "传 0.0 应能关掉 MAD 兜底")
    check("by_mad" not in off.cache.to_debug(),
          "关着时不该出现 by_mad（免得让人以为它在起作用）")


# ─────────────────────────── 3. 安全阀 ───────────────────────────

def test_resolution_change_invalidates_the_cache() -> None:
    """换了分辨率（模拟器改了缩放）→ 槽位换算的裁剪区就不是原来那块了，
    缓存必须整份作废，不能靠「hash 撞上了」跨场景复用读数。"""
    profile = Profile.load(WPK)
    engine = StubEngine({})
    stream = RoiStream(profile, engine, with_dealer=False)

    stream.read(make_marked((1080, 240)), frame_id="f1")          # 尺寸 A
    frame2 = stream.read(make_marked((540, 120)), frame_id="f2")  # 尺寸 B
    check(stream.stats.resets == 1, f"换分辨率应触发一次作废，实际 {stream.stats.resets}")
    check(frame2.n_reused == 0, f"作废后不该有复用，实际复用 {frame2.n_reused}")

    frame3 = stream.read(make_marked((540, 120)), frame_id="f3")  # 尺寸 B 再来一帧
    check(frame3.n_reused == frame3.n_roi,
          f"同尺寸的第二帧应全部复用，实际 {frame3.n_reused}/{frame3.n_roi}")


def test_threshold_change_invalidates_the_cache() -> None:
    """门槛也是签名的一部分：调了门槛就得重新建缓存，否则统计和实际不符。"""
    profile = Profile.load(WPK)
    engine = StubEngine({})
    stream = RoiStream(profile, engine, with_dealer=False)
    img = make_marked()
    stream.read(img, frame_id="f1")

    stream.cache.max_distance = 5               # 绕过签名地改门槛 = 模拟「调了参数忘了重建」
    frame2 = stream.read(img, frame_id="f2")
    check(stream.stats.resets == 1, "门槛变了应触发作废")
    check(frame2.n_reused == 0, "作废后第一帧不该复用")


def test_dedup_off_runs_rec_every_frame() -> None:
    """dedup=False：缓存为 None，同一张图也每帧重跑 rec，结果仍然正确。"""
    profile = Profile.load(WPK)
    img = make_marked()
    engine = StubEngine(build_table(profile))
    stream = RoiStream(profile, engine, with_dealer=False, dedup=False)
    check(stream.cache is None, "dedup=False 时 cache 应为 None")

    first = stream.read(img, frame_id="f1")
    calls_after_first = len(engine.crops)
    second = stream.read(img, frame_id="f2")
    check(len(engine.crops) == calls_after_first * 2,
          f"关去重后第二帧应重跑全部 rec，实际从 {calls_after_first} "
          f"变成 {len(engine.crops)}")
    check(second.n_reused == 0,
          f"关去重后不该有复用，实际 {second.n_reused}/{second.n_roi}")
    check(first.text("name", "hero") == second.text("name", "hero") == "hero:name",
          "两种路径读出的文本应一致")


def main() -> int:
    tests = [
        test_one_changed_digit_in_long_text,
        test_tiles_are_what_makes_the_default_safe,
        test_identical_crops_hash_equal,
        test_narrow_roi_is_not_tiled,
        test_pixel_mad_basics,
        test_hash_grid_must_fill_whole_bytes,
        test_unchanged_frame_runs_no_rec_at_all,
        test_changed_roi_reruns_and_neighbours_reuse,
        test_reused_read_is_a_copy,
        test_low_score_read_ignores_mad_fallback,
        test_empty_read_stays_cheap,
        test_photo_noise_is_not_treated_as_uncertain_text,
        test_low_score_read_cannot_be_pinned,
        test_pinned_key_ignores_pixel_changes_until_unpinned,
        test_unpin_single_key_restores_reread,
        test_mad_eps_defaults_to_0_2,
        test_resolution_change_invalidates_the_cache,
        test_threshold_change_invalidates_the_cache,
        test_dedup_off_runs_rec_every_frame,
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
    print("\n✅ ROI 去重全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
