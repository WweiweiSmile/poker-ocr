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
nv tools/ab_range_set.py --check

# 跑全部 ready 用例（事实层，不调模型）
nv tools/ab_range_set.py

# 只跑 P0；同时跑旧/新模型（慢，需要 AI 凭据）
nv tools/ab_range_set.py --priority P0
nv tools/ab_range_set.py --model --priority P0

# 落盘
nv tools/ab_range_set.py --json tools/ab_results.json
```

控制台只读页（复跑控制台）：`http://127.0.0.1:8010/range-ab`。
它读 `tools/ab_set.json` + `tools/ab_results.json`，不跑模型、不碰 DB；
没有结果文件时只显示清单里记录的旧/新权益。

页面上每条用例可展开：**行动线**（每条街、每个人的动作，含推断/全下标）+ 三张
建议卡（线上原建议 / 旧引擎重跑 / 新引擎重跑，含理由）。模型建议来自
`ab_range_set.py --model --json tools/ab_results.json`，是单次采样，可能波动。

`expect` 的含义：`cross_down`（旧过门槛、新不过）、`cross_up`（旧不过、新过）、
`stay_above` / `stay_below`（两侧同侧）、`n/a`（无门槛，如自己下注）。
模型建议有随机性，`--model` 默认只记录与 `new_advice` 的差异，不判 FAIL；
要硬判加 `--strict-advice`。

结果行带 `prompt_version`，每个路径再带 `*_source`（model / fallback）与
`*_finish_reason`。跨提示词版本比结果时先看这三个字段：提示词换了、这次跑成
fallback、或输出被 `MAX_TOKENS` 截断，看起来都可能像「建议变了」。

## 2. 用例表

| id | 手牌 | 决策点 | 类别 | 旧 → 新权益 | 门槛 | 预期 | 状态 |
|---|---|---|---|---|---|---|---|
| P0-01 | `20261101-d32f55ecc1` | river idx=16 | value_vs_multiway_donk_jam | 69.2% → 5.4% | 24.1% | cross_down | ready |
| P0-02 | `20261101-d32f55ecc1` | river idx=18 | value_vs_raise_over_bet_call | 59.7% → 1.0% | 11.2% | cross_down | ready |
| P0-03 | `20261101-b3c7d710f7` | river idx=13 | self_lead_not_donk | 69.6% → 40.6% | 24.9% | stay_above | ready |
| P0-04 | `20261030-4d6df87e6e` | flop idx=9 | middle_pair_multiway | 13.2% → 17.6% | 22.6% | stay_below | ready |
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

### 2.1 数据修正记录（2026-11-02）

- `20261030-4d6df87e6e`（P0-04）：落库时大盲被判成「翻前弃牌」——实际是
  1BB 已投、无人加注的默认过牌；hero 的翻牌弃牌也没记。已按帧/筹码/底池
  对账修正：大盲改 `check`，补 hero 翻牌 `fold`，`street_active` 翻牌补
  BB、turn/river 去掉 hero。修正后 P0-04 从 `cross_up`（22.3%→28.2%）
  变为 `stay_below`（13.2%→17.6%），模型两路都 fold；原 cross_up 是
  「假弃牌」把 BB 从范围里丢掉造成的。
- 对应源码修复见 `2bcbef1`：弃牌改为「手牌图标消失 + 头像中间『弃牌』」
  双条件，头像块走 det+rec（rec-only 会把大号白字读成 奔牌/井脚/空）。

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

## 6. 摊牌证据（witness）A/B 与 30 点标注（#38，2026-11-16）

### 6.1 怎么跑的（与 §15.3 的三处偏差都写在这里）

- **开关**：`WITNESS_PROMPT=1/0`（`poker_ocr/witness/render.py:143` 的 `enabled()`），同一份代码、同一份画像快照、同一手牌集，只切这一个环境变量。
- **画像快照**：`data/opponents/profiles.json` 现在**没有 witness 块**（§5–§8 投影落盘还没实现），所以两遍共用一份合成快照 `logs/ab/profiles_witness.json` = 仓库快照 + 用 781 手 `data/hand_outbox/sent/*.json` 经 `admission.iter_rows` 推出的 witness 块（356 行 / 269 admitted / 66 名玩家）。两遍读同一个文件，「同输入」成立；仓库里的 profiles.json 未被改动。
- **离线链路补注入**：`tools/replay_live_decisions.py` 原先根本不传 `witness_by_seat`，A/B 会测到 0 差异。本轮把 `live.py` 的座位排序/预算逻辑提到 `poker_ocr/witness/render.py::witness_by_seat`，live 与复跑共用一份；复跑在 `build_situation` 前调用它，并把每点的行写进 `--json` 的 `witness` 字段（`tests/test_witness_render.py` 有对应断言）。
- **手牌集不人工挑**：`runs/*/live.states.jsonl` 里所有能复跑的 advice 记录全量入选，7 个 run 合并去重后 **121 个可比决策点**（75 手）。合并用 `nv tools/ab_merge.py logs/ab/*.w1.json -o logs/ab/merged-w1.json`（键与 ab_diff 同源）。
- **§15.3 的自动筛选（`--witness-only`）**：口径是「至少一个在局对手落在 witness 覆盖窗口、有 admitted 行」，落地代理 = 本点的 `witness` 字段非空。筛后 **n=120**（唯一被筛掉的点是 `20261112-7818d3240f`，`opponents` 为空，本来就不该进 A/B），翻转 6/120 = 5.0%，噪声 10/120 = 8.3%，超额仍是 **−3.3pp**——筛选不改变结论。注意这一筛不能对 w0 臂做（关掉时 `witness` 恒空，会筛成 0 点），只能拿 w1 臂筛出的键去约束两臂。
- **与 ready 集的交叉核对**：本文件 §2 表里的 14 个 ready 手键与本轮 121 点**零交集**——ready 集是 10-30 / 11-01 的手，本机可复跑的 `runs/*` 只有 11-12 的局。这是数据可得性边界，不是抽样偏差。
- **跑法**：`WITNESS_PROMPT=<0|1> nv tools/replay_live_decisions.py --log runs/<run>/live.states.jsonl --hands logs/ab/hands.json --profile-db logs/ab/profiles_witness.json --model --json logs/ab/<run>.w<arm>.json`，deepseek-flash、thinking=disabled、temperature=0.2；再 `nv tools/ab_diff.py logs/ab/merged-w0.json logs/ab/merged-w1.json --json-out logs/ab/diff-w0-w1.json`。
- **偏差：对照组塌了**。§15.3 的主指标要「事实块未变」子集当基线，但 witness 覆盖饱和——121 点里 120 点事实块都变了（且只因 witness 行而变），未变子集 **n=1**，基线没有统计意义。改用**同配置重跑一遍**（第三臂 `w1b`，同快照同开关）估模型噪声：超额 = 翻转率(变了) − 翻转率(同配置重跑)。这是明确替换，不是悄悄改口径。
  `tools/ab_diff.py` 也据此改了：基线子集 n<30 时**不再打印超额**（打印 `—` + 一行警告），免得 n=1 的「超额 +5pp」被当成结论；本轮的超额 −3.3pp 是拿 `merged-w1.json` 对 `merged-w1b.json` 手算的（`nv tools/ab_diff.py logs/ab/merged-w1.json logs/ab/merged-w1b.json --show 0`，翻转率那一行即噪声臂）。

### 6.2 结果

| 指标 | 值 |
|---|---|
| 可比决策点 | 121（两次独有 0） |
| 动作翻转（witness 开 vs 关） | 6/121 = **5.0%**（95% CI 1.1–8.8%） |
| 其中同族换尺度 / 换动作类 | 5 / 1（唯一换类：`20261112-1fc64a990f` 翻前 63o call→fold） |
| 事实块变化 | 120/121（99%），段落只动「对手画像」 |
| 行级归因 | 只因 witness 行而变 120 点，平均段内 +6.5 行；其它行变化 0 |
| 同配置重跑噪声 | 10/121 = **8.3%**（95% CI 3.4–13.2%） |
| **超额翻转率** | **−3.3pp**（5.0% − 8.3%），远低于 §15.6 的 10pp 判失败线 |
| 分街翻转（开/关） | 翻前 68 点 2 例 3%；翻牌 27 点 0；转牌 16 点 2 例 12%；河牌 10 点 2 例 20% |
| 证据覆盖 | 个人证据 71 点 / 只有池级 49 点 / 无证据 1 点；平均 6.74 行/点 |
| §15.3 筛选后（`--witness-only`） | n=120，翻转 6/120 = 5.0%，噪声 10/120 = 8.3%，超额 **−3.3pp**（同上） |

逐点明细在 `docs/AB测试-摊牌证据-121点归因.csv`（121 行，一点一行）：`动作变化` 列求和 = 6、`只因 witness 行而变` 求和 = 120、`事实块变化` 求和 = 120，上表每个数字都能在表里逐列加出来。重新生成：

```bash
nv tools/ab_diff.py logs/ab/merged-w0.json logs/ab/merged-w1.json \
    --json-out logs/ab/diff-w0-w1.json --csv-out docs/AB测试-摊牌证据-121点归因.csv
```

读法：n=121 下 5.0% 与 8.3% 的置信区间重叠，**这一轮测不出 witness 行对动作有可分辨的影响**（既没测出收益，也没测出伤害）。

§15.6 三条失败判据的机器侧状态，别混着说：

1. 超额 ≥10pp —— **未命中**（−3.3pp，机器可复算）。
2. 人工 30 点里「反被推歪」≥2 例 —— **还没有读数**，CSV 的 (a)(b)(c) 三列全空，等 §6.4 签字。
3. 出现 1 例「压过硬事实」—— **还没有读数**，同上；而且这 30 点里 9 个对照点本身也带证据行，对照侧样本不足（见 6.3）。

所以现在能说的只是「第 1 条没命中」；第 2、3 条要等人工标完才有结论，不能提前写成「三条都没命中」。

### 6.3 30 点人工标注

- 表：`docs/AB测试-摊牌证据-30点标注.csv`（30 行，含证据行原文、两遍的建议与理由、三个待填判读列）。
- 构成：**6 个翻转点**（全部入表）+ 14 个「个人证据」抽样点 + 9 个「只有池级证据」对照点 + 1 个「无证据」对照点。
- **§15.4 要求的 10 个「无 witness 行」对照点在当前数据里凑不出**：121 点里只有 1 点真的没有证据行（覆盖饱和），所以 9 个对照位用「只有池级、没有个人证据」的点补，并单列「证据层级」标清。
- 抽样由脚本按街定额等距取（翻前 7 / 翻牌 4 / 转牌 2 / 河牌 1），没有挑好看的。

机器先跑出来的三条观察（供人工判读时对照，不代替签字）：

1. **池级句按座位重复**。§10.2 的口径是「个人无、池级 n≥20 → 出池级句」，逐座位渲染，于是一个 4 人池里同一句「这个池子在河牌的亮牌里两对+占 56%（132 次，本池）」会出现 4 次；表里 #7 一个翻前决策点带了 15 行证据，其中 10 行是两条池级句各重复 5 次，600 字预算被它吃掉大半。这是口径问题（要不要改成每点最多一条池级句），本轮没改，等票主定。
2. **证据的街与决策的街不匹配**。翻前决策点上出现的是「他在河牌 OOP 过牌时亮过打牌面（2 次）」——§10.2 的排序只是「同 spot 优先」，没有要求同街，跨街行合规；但翻前拿河牌摊牌数据当依据，可解释性弱。
3. **模型几乎不引用证据**。121 个新理由里含「亮过 / 摊牌 / 本池 / 亮牌」的只有 1 条，且那条说的是 Hero 自己的摊牌价值（误命中）。即 (b) 维度在全部 121 点上都是「未被引用」——动作偶尔变，理由里看不到证据。这是 §15.7「机器证明不了有用」的具体形态。

### 6.4 待人工签字

按 §15.7，30 点由本人逐行读完 CSV 的三个判读列，在实现票上留一行「已读，X 例误用」，再关票。机器侧（单测、golden、A/B 阈值）已齐。