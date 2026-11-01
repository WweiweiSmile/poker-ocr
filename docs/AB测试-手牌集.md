# A/B 测试：对手范围事件模型手牌集

2026-11-01。清单 [`tools/ab_set.json`](../tools/ab_set.json)；跑批 [`tools/ab_range_set.py`](../tools/ab_range_set.py)；
控制台只读页 `http://127.0.0.1:8010/range-ab`。

## 0. 用途

对比两条路径：

- **baseline**：`RANGE_EVENT_MODEL=0`，旧口径（一条街一个 mode、一次过滤）；
- **candidate**：`RANGE_EVENT_MODEL=1`，事件模型（按动作时间顺序更新范围）。

每条用例记录：手牌、决策点、旧/新权益、赔率门槛、预期行为（跨门槛/保持）。
14 条 `ready` 覆盖目标修复、回归守卫、听牌/短码、多人池、河牌只赢空气；
8 条非 `ready` 记录了数据缺口，修好后启用。

## 1. 怎么跑

```bash
# 校验清单与本地手牌
.venv/bin/python tools/ab_range_set.py --check

# 跑全部 ready 用例（事实层，不调模型）
.venv/bin/python tools/ab_range_set.py

# 只跑 P0；同时跑旧/新模型（慢，需要 AI 凭据）
.venv/bin/python tools/ab_range_set.py --priority P0
.venv/bin/python tools/ab_range_set.py --model --priority P0

# 落盘
.venv/bin/python tools/ab_range_set.py --json tools/ab_results.json
```

控制台只读页（复跑控制台）：`http://127.0.0.1:8010/range-ab`。
它读 `tools/ab_set.json` + `tools/ab_results.json`，不跑模型、不碰 DB；
没有结果文件时只显示清单里记录的旧/新权益。

`expect` 的含义：`cross_down`（旧过门槛、新不过）、`cross_up`（旧不过、新过）、
`stay_above` / `stay_below`（两侧同侧）、`n/a`（无门槛，如自己下注）。
模型建议有随机性，`--model` 默认只记录与 `new_advice` 的差异，不判 FAIL；
要硬判加 `--strict-advice`。

## 2. 用例表

| id | 手牌 | 决策点 | 类别 | 旧 → 新权益 | 门槛 | 预期 | 状态 |
|---|---|---|---|---|---|---|---|
| P0-01 | `20261101-d32f55ecc1` | river idx=16 | value_vs_multiway_donk_jam | 69.2% → 5.4% | 24.1% | cross_down | ready |
| P0-02 | `20261101-d32f55ecc1` | river idx=18 | value_vs_raise_over_bet_call | 59.7% → 1.0% | 11.2% | cross_down | ready |
| P0-03 | `20261101-b3c7d710f7` | river idx=13 | self_lead_not_donk | 69.6% → 40.6% | 24.9% | stay_above | ready |
| P0-04 | `20261030-4d6df87e6e` | flop idx=9 | middle_pair_multiway | 22.3% → 28.2% | 22.6% | cross_up | ready |
| P0-05 | `20261030-2e9aeb101c` | flop idx=12 | oesd_vs_raise_allin | 29.4% → 30.9% | 33.3% | stay_below | ready |
| P0-06 | `20261030-acf43ea34f` | river idx=17 | full_house_vs_lag_jam | 91.0% → 70.9% | 18.2% | stay_above | ready |
| P1-01 | `20261101-bb24534caa` | flop idx=12 | overpair_vs_short_jam | 41.4% → 54.6% | 14.5% | stay_above | ready |
| P1-02 | `20261101-966c713ef9` | turn idx=14 | ace_high_gutshot_vs_turn_bet | 17.8% → 32.5% | 28.0% | cross_up | ready |
| P1-03 | `20261030-085482f910` | flop idx=3 | bomb_pot_short_jam | 1.3% → 1.6% | 38.0% | stay_below | ready |
| P1-04 | `20261101-6a015212ad` | river idx=15 | only_beats_air_vs_station | 0.0% → 3.1% | 24.8% | stay_below | ready |
| P1-05 | `20261030-5f40d520d2` | flop idx=8 | top_pair_value_call | 52.7% → 62.3% | 22.7% | stay_above | ready |
| P1-06 | `20261030-5c1b22f210` | turn idx=10 | bomb_pot_top_pair | 10.8% → 14.7% | 25.0% | stay_below | blocked_cap |
| P2-01 | `20261030-f7216b7a45` | flop idx=3 | nine_way_bomb_pot | 3.3% → 2.5% | 25.5% | stay_below | ready |
| P2-02 | `20261030-052bbe89a7` | flop idx=1 | seven_way_bomb_pot_nut_flush_draw | 37.0% → 37.0% | — | n/a | ready |
| P2-03 | `20261030-a24e872847` | river idx=15 | air_on_four_flush_board | 0.0% → 6.9% | 25.0% | stay_below | ready |
| P2-04 | `20261030-f2f71f65f2` | flop idx=8 | set_on_paired_board | 53.1% → 53.1% | — | n/a | data_broken |
| P2-05 | `20261030-a334dcaf48` | flop | oesd_vs_raise | 32.0% → — | 25.0% | stay_above | manual |
| P2-06 | `20261030-0c2ec0fc88` | flop | ak_3bet_pot_state_bug | — → — | — | n/a | blocked_data |
| P3-01 | `h4` |  | air_as_value_two_barrels | — → — | — | n/a | needs_reconstruction |
| P3-02 | `h75` |  | counterfeited_two_pair_bomb_pot | — → — | — | n/a | needs_reconstruction |
| P3-03 | `h100` |  | counterfeited_two_pair_river | — → — | — | n/a | needs_reconstruction |
| P3-04 | `h104` |  | pot_odds_contradiction | — → — | — | n/a | needs_reconstruction |

## 3. 状态含义

| status | 含义 |
|---|---|
| `ready` | 本地有手牌、可回放；默认模式重算旧/新权益并核对预期 |
| `blocked_cap` | 4 人建模上限按座位顺序截断，真正的下注者被丢掉（5c1b、f7216） |
| `blocked_data` | 状态帧/行动线与手牌记录矛盾（0c2e、e860） |
| `data_broken` | 行动线断裂（f2f 转牌「LJ 下注→LJ 弃牌」） |
| `manual` | 无 advice/state，只能手工重建（a334） |
| `needs_reconstruction` | 10-23 复盘手，记录缺街/无 action_index（h4/h75/h100/h104） |

## 4. 已知缺口（按优先级）

1. **4 人建模上限按座位截断**：`field_ranges.resolve` 的 `active[:4]` 会让 9 人炸弹池里
   第 5 位之后的下注者掉出模型（5c1b 的 UTG bet 22.5、f7216 的 UTG 65）。
   建议按动作优先级选：最后加注者 → 跟注者 → 其他人。修完后 5c1b 从
   `blocked_cap` 转 `ready`，多人池 A/B 才有意义。
2. **`RFI_BB` 缺失**：BB 翻前加注者没有对应先验，`_lookup` 直接跳过该座位
   （55e5 的河牌全下者、f1e3）。补一个 BB 隔离加注范围，或 fallback + warning。
3. **a334 手工重建**：10-31 复盘已把门槛算对（25%，32% → call），但手牌没有
   advice/state，需要按复盘文档重建快照后才能进自动 A/B。
4. **10-23 复盘手**：`hands/1023.hands.json` 缺 `seat_meta`/`street_active`/
   `action_index`，部分街缺失、牌面误读；要写转换器或手工重建。它们的根因类别
   已由 outbox 等价手覆盖（b3c7d710f7、acf43ea34f、966c713ef9、4d6df87e6e、
   2e9aeb101c、6a015212ad）。

## 5. 覆盖边界

- 手牌库 304 个决策里 179 个翻前、翻后 125 个、河牌只有 19 个；本集河牌 7 条。
- 缺少「多人池河牌加注」「河牌 check-raise」「短码河牌 jam」这几类线路；
  当前结论只能说明「没有回归」，不能说明「新模型在这些线路上更好」。
- 建议后续从视频/手牌库补这些线路，再扩本集。
