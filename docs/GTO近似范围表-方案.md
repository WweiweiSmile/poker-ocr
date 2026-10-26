# 翻后 GTO 近似范围表：跑树方案 + AI 接入方案

> 目标：离线用开源求解器，把「常见翻后局面 × 常见牌面 × 5 档底池尺度」的均衡策略
> 提前跑成一张可查询的表；实时建议时按当前牌面/底池/位置/手牌做一次 <10ms 的查询，
> 把「这手牌在这个牌面该下注还是过牌、五档里哪档 EV 最高（两档插值）、高多少」作为**事实**
> 注入提示词，让模型不用猜牌面结构。
>
> 机器假设：Ryzen 9 5900X（12 核 24 线程，DDR4 双通道），单机离线跑。

---

## 0. 先说结论

1. **引擎不用另选**：`gto/vendor/postflop-solver` 已经是 b-inary 的 Rust 求解器
   （Discounted CFR + rayon 多线程 + 16-bit 压缩），API 足够（按手牌读策略、读 EV、
   `play()` 走树、chance 节点取转牌）。用它，TexasSolver 只当交叉验证。
2. **翻牌 5 档，转河 3 档（1/3、2/3、1.2）**，并关掉自动全下
   （`add_allin_threshold=0`）。转河只放 2 档会缺「面对 1/2 下注」这类高频防守节点
   （App 自己就常建议 1/2）；5 档全街则要乘 6 倍以上。3 档是成本与覆盖的平衡点，
   缺的 1/2、1.0 用相邻档插值并标 `approx`，见 §6.5。
3. **牌面不全量跑**：22,100 个翻牌按花色同构收敛成 1,755 个，再按纹理聚成
   **184 类**，每类解一个代表牌面。线上用同构映射命中代表牌面——同构范围内是
   **精确**的，只有跨类才是近似，所以查询结果必须带 `confidence`。
4. **5900X 预算**：单牌面 × 单场景，转河 2 档约 **4–8 分钟**、3 档约 **8–18 分钟**；
   标准表（184 类 × 3 场景）分别约 **37–74 小时** / **74–166 小时**。
   先花 1 天做「2/3/5 档消融」，用 EV 数据决定要不要付这 2 倍，见 §6.5、§11。
5. **接入点只有一个**：`Situation` 加 `gto` 字段，`render_hand_facts()` 里多渲染
   一段 `## GTO baseline`。选项生成（`build_options`）、点击器、OCR 全都不动。

---

## 1. 这张表要回答什么、不回答什么

| 回答 | 不回答 |
|---|---|
| 这个牌面类别下，双方范围的**权益/坚果优势** | 多人池（3 人以上）的均衡策略 |
| 我这个**具体手牌**在已解尺度下的频率、以及五档的 EV（两档插值） | 翻前策略（继续用 `preflop_ranges.py`） |
| 整个范围的**下注频率 / 尺度组合 / 过牌范围** | 任意筹码深度的精确解（按档位近似） |
| 被加注后 call / 3-bet / fold 的基准 | 带抽水的精确解（抽水单独配，见 §6） |
| 「哪些牌会用 1.2 倍池」这类**结构性**事实 | 对手具体手牌 |

**边界必须写进提示词**：这张表是「对标准范围、单挑、无抽水/固定抽水」的基准，
不是「这桌就该这么打」。桌面动态（`.table_dy`、对手画像）仍然优先。

---

## 2. 为什么不能把「整棵决策树」直接存下来

先算一笔账，避免走弯路：

- 一个 3 条街、翻牌 5 档尺度的树，节点数在 **10⁴–10⁵** 量级；
- 每个节点要存 1,326 种组合 × 6 个动作（5 档 + 过牌/弃牌/跟注）≈ 8 KB（int8 量化）；
- 单牌面 ≈ 100 MB–1 GB；184 类 × 5 场景 ≈ **TB 级**。

所以正确做法是**「算一次、抽干、丢树」**：

1. 求解器在内存里跑完整棵树；
2. 只抽取「会真的被查询到」的节点：
   - 每条街的**街起点**（没人下注）；
   - **面对已解尺度**的响应节点（翻牌 5 档、转河 3 档）；
   - 少量常见线路（翻牌 check-check、bet-call 之后的转牌起点）；
3. 抽成「按手牌类别 + 量化频率」的扁平数组，写进 SQLite；
4. 原始树（`.bin`）只保留最近 N 个用于复现，其余删掉。

这样标准表（184 类 × 3 场景）大约 **4 GB**，单场景约 1.3 GB；查询是主键命中，和「树有多大」无关。

---

## 3. 引擎与许可

| | postflop-solver（已 vendor） | TexasSolver |
|---|---|---|
| 语言/并行 | Rust，rayon，SIMD | C++，OpenMP |
| 算法 | Discounted CFR (γ=3)，4 的幂次重置累计策略 | CFR 变体 |
| 优势 | 已在仓库里、API 细（能按手牌读 EV/策略）、压缩选项 | 有公开 benchmark、和 Pio 对齐过 |
| 许可 | **AGPL-3.0-or-later** | **AGPL-3.0** |

**许可提醒**：自己用、不对外分发没问题；一旦要把这个服务/客户端打包给别人，
AGPL 要求开源对应代码。真要商业化，得换商业求解器或自己重写引擎。

**验证策略**：用同一个牌面、同一套范围，跑 postflop-solver 和 TexasSolver 各一次，
比较根节点的 bet% 与平均 EV。差异 > 3 个百分点就说明树配置或范围写错了。

---

## 4. 场景矩阵（spot）

一个 spot = 「翻前动作线 + 位置对 + 有效筹码」。现场 9 人桌优先做这几个：

| 优先级 | spot | 翻前底池（按本桌 6BB 下限） | 翻牌后手 / SPR | 说明 |
|---|---|---|---|---|
| P0 | SRP：BTN 开池 6BB vs BB 跟注 | 12.5BB | 94BB / 7.5 | 最高频 |
| P0 | SRP：CO 开池 6BB vs BB 跟注 | 12.5BB | 94BB / 7.5 | 次高频 |
| P1 | 3BP：BTN 开池 6BB，BB 3-bet 到 18BB，BTN 跟注 | 36.5BB | 82BB / 2.2 | 大池决策 |
| P1 | 3BP：BB 对 BTN 开池 3-bet 到 18BB，BTN 跟注 | 36.5BB | 82BB / 2.2 | 位置反过来 |
| P2 | 溜入：SB 补盲 vs BB 过牌 | 2BB | 99BB / 49.5 | SPR 极高，尺度另算 |
| P2 | 4BP：BTN 4-bet 到 45BB，BB 跟注 | 90.5BB | 55BB / 0.6 | 基本是翻牌全下 |

**两个必须现在就定下来的口径**：

1. **用本桌真实的翻前尺度，不要用 2.5BB 开池的通用 GTO 配置。**
   本桌最低加注 6BB（`advice.PREFLOP_MIN_RAISE_BB`），翻牌底池是 12.5BB 而不是 5.5BB，
   SPR 差 2.4 倍。用错底池，整张表的「什么牌能打光」全会错。
2. **范围要和 App 的世界观一致**：`preflop_ranges.py` 是松弱桌口径（BTN RFI 43%）。
   如果求解器用标准 GTO 范围（BTN ~45% 但结构不同、BB 防守更紧），表会给出
   「你这手不该在范围里」的结论，和现有建议打架。
   建议：**每个 spot 存两份范围**（`gto` 与 `app`），默认用 `app`，A/B 时切 `gto`。

范围导出：把 `preflop_ranges.RFI` 的 169 键展开成 1,326 组合，写成求解器认识的
字符串（`AA,KK,AKs,...`），并对 BB 防守范围单独维护一张表（现有代码没有 BB 防守表，
这是本方案要先补的数据）。

---

## 5. 牌面抽象：22,100 → 1,755 → 184

```
22,100 个翻牌
   │  花色同构（3 张牌的花色相等关系只有 5 种模式）
   ▼
1,755 个代表翻牌（精确，无信息损失）
   │  纹理聚类（k-medoids）
   ▼
184 个牌面类（近似；每类一个 medoid 代表牌面）
```

**特征向量**（用来聚类，也用来给模型解释牌面）：

| 维度 | 取值 |
|---|---|
| 对子 | 无对 / 一对 / 三条 |
| 花色 | rainbow / two-tone / monotone |
| 连通性 | 最大间隙、是否已成顺、卡顺/两头、隔张 |
| 高牌 | A / K / Q / J / T / 9-6 / 5-2 |
| 大牌数量 | broadway 张数（0–3） |
| 坚果相关性 | 是否有 A/K 同花、是否阻断坚果顺 |

**线上查询**：
1. 拿到实际翻牌（如 `Ks9s4h`）→ 按花色同构映射到 1,755 里的规范代表（`Kd9d4c` 一类）；
2. 规范代表 → 所属 184 类 → 命中该类的 medoid 解；
3. 如果是 medoid 本身，`confidence=exact`；否则 `confidence=class`（近似）。

> 184 这个数字来自常见的翻牌分类（知乎/PLO 教材都用它）。**不要直接抄外部列表**，
> 用上面的特征自己聚一遍：跑 10 个类做抽样，比较类内两个牌面的根节点 bet% 差异，
> 中位数 < 5 个百分点就说明 184 够用；不够就加到 250–300，成本线性增加。

**转牌 / 河牌**：

- 转牌：49 张牌，**全部精确存**（求解器本来就算过），按 `(翻牌类, 转牌)` 建索引；
  存储不够时再压成 12 个桶（A/K/Q/J/T、9-6、5-2 × 完成同花/配对/空白）。
- 河牌：2,352 个 runout，全存约 100 GB/场景，**不存**。
  按 10 个桶存，或复盘时用同一棵树按实际河牌重放（`play(card)`）现取。

---

## 6. 树配置（最关键的一段）

```jsonc
{
  "engine": "postflop-solver",
  "spot": "SRP_BTN_vs_BB_100bb",
  "starting_pot": 12.5,          // 本桌 6BB 开池后的翻牌底池
  "effective_stack": 94.0,       // 翻牌时后手
  "rake_rate": 0.0,              // 现场局先按 0；要模拟抽水见下
  "rake_cap": 0.0,
  "flop_bet_sizes":  ["33%, 50%, 67%, 100%, 120%"],
  "turn_bet_sizes":  ["33%, 67%, 120%"],   // 3 档：小/中/大；1/2、1.0 靠插值
  "river_bet_sizes": ["33%, 67%, 120%"],
  "raise_sizes":     ["a"],      // M0 只保留全下加注；3 档加注内存 12GB+、时间 ×8
  "add_allin_threshold": 0,      // 关键：不要自动补一个 16 倍池的全下分支
  "force_allin_threshold": 0.15, // SPR 很低时仍然强制全下
  "merging_threshold": 0,        // 不要合并 1.0 和 1.2，否则没法比 EV
  "compression": true,           // 16-bit 策略存储，内存减半
  "max_iterations": 250,
  "target_exploitability_pct_pot": 0.4
}
```

**为什么这样配**：

- `add_allin_threshold: 0`：100BB、SPR 7.5 时，1.2 倍池只有 15BB，离 94BB 的后手很远。
  自动补全下 = 白送一整棵子树，还让策略出现「用 6 倍池全下」的噪声。
- `merging_threshold: 0`：合并会把相邻档（尤其 1.0 与 1.2）并成一档，
  表里就查不到用户要的那个选项了；近似应该来自「档少 + 插值」，不是悄悄合并。
- `raise_sizes` M0 只放 `a`（全下）：实测 3 档加注会把 591 手的内存从 3.5GB 拉到
  12GB+、单次时间 ×8。下注尺度是这张表的主目标，加注尺度先用「全下」兜底；
  M2 再加 1 档 67%。另外两边公式不同——App 是
  `加注到 = 最高下注 + f × (底池 + 需跟额)`，求解器的 `PotRelative` 是
  `加注到 = 上家下注 + f × 节点底池`，差一个「需跟额」。加回加注档时必须写单测钉死口径。
- 迭代 250：实测（591 手、3 档）100 次到 2.83% 底池，250 次约 1.1–1.5%，
  对近似表够用；想到 0.4% 要 600 次以上（时间 ×2.4）。先 250，抽 3 个 job 看
  exploitability 再决定。

**抽水**：现场局通常 5% 封顶（比如 3BB）。抽水会显著收紧跟注范围和薄价值。
第一版先跑无抽水（和 GTO 视频对照方便），第二版加 `rake_rate=0.05, rake_cap=3`，
单独打一个 `_rake` 版本的库，用开关切换。

### 6.5 转河到底几档：2 / 3 / 5 的取舍

先纠正一个直觉：**尺度的作用不只是「我下多大」，更是「他下这个大小我怎么防」**。
求解器树里的下注档对双方都生效。App 自己经常建议 1/2 池；表里如果没有 1/2 节点，
「转牌面对 1/2 下注」——最高频的防守场景之一——就查不到。这比「我少一个下注选择」
严重得多。

成本是乘法。设翻牌 F 档、转牌 T 档、河牌 R 档，河牌层（最贵的部分）≈ F×T×R：

| 配置 | 相对成本 | 单次（5900X） | 184×3 标准表 | 能回答什么 |
|---|---|---|---|---|
| 翻 5 / 转 2 / 河 2 | 1.0 | 4–8 min | 37–74 h | 缺 1/2、1.0 的攻防节点 |
| **翻 5 / 转 3 / 河 3** | **2.25** | **8–18 min** | **74–166 h** | 缺 1/2、1.0（响应靠插值） |
| 翻 5 / 转 5 / 河 5 | 6.25 | 25–50 min | 230–460 h | 全覆盖 |

（每多一档，按 `(n+1)/n` 乘，两条街复合；真实倍数用下面的消融校准。）

注意：3 档只解了 1/3、2/3、1.2 三个节点。**面对 1/2 下注时，响应策略是靠 1/3 与 2/3
插值出来的**——赔率门槛从 20% 到 28.6%，插值可用但不精确，必须标 `approx`。
如果现场 1/2 下注特别多，两个选择：把 3 档换成 4 档（1/3、1/2、2/3、1.2，丢掉 1.0），
成本约 4 倍；或只对 top 30 牌面类做 5 档精细表，查询优先命中精细层。

参考：GTO Wizard 的默认解一般每街 2–3 档，5 档算多。多数牌面的 EV 曲面在最优尺度
附近很平，多一档的 EV 差通常 < 0.2–0.5BB；真正会变的是**频率分布**——小注/阻挡注
会不会从策略里消失。

**默认选 3 档（1/3、2/3、1.2）**，理由：

1. 覆盖「小 / 中 / 大」三个极化区间，正好是策略最常分的三类；
2. 1/2、1.0 落在相邻解之间，EV 可以插值（曲面平滑），策略频率不插值、只标 `approx`；
3. 比 2 档只贵 2.25 倍，比 5 档便宜 2.8 倍。

**不要拍脑袋，先做 1 天消融**：

- 10 个代表牌面（A 高干燥、K 高两同花、连张、公对、单花、低牌面各 1–2 个）；
- 每个牌面跑 3 套：转河 2 档 / 3 档 / 5 档，迭代 150；
- 量三个数：根节点 `bet vs check` 的 EV 差；每手牌策略的 L1 距离；
  「只允许在 2 档里选」相对 5 档的 EV 损失；
- 判据：2 档的 EV 损失 < 0.2BB 且频率偏移 < 5 个百分点 → 2 档够用；
  否则直接上 3 档；5 档只在「精细层」（top 30 牌面类）再用。

消融约 10–20 小时，能省掉后面几天的盲目计算，也决定 §11 的总时长按哪一行报。

### 6.6 能不能先跑 3 档、以后再补到 5 档？

可以，但要先认清两件事：

1. **补档 = 重跑，不是追加。** CFR 的均衡是整棵树一起定的：新增一个下注档会改变
   同一节点上其他档的 regret，也会改变范围在下一街的构成。3 档树的解不能直接
   「长出」第 4、5 档，加档必须按新树重算。所以 v1 的机时**不能抵扣** v2。
2. **先解哪三档，看「覆盖」不看「频率」。** 1/3、1/2、2/3 是当前 AI 的偏好
   （提示词就这么写的），不是 GTO 的分布；而且 1.0/1.2 落在 2/3 **外面**，只能外推——
   EV 曲线在 overbet 区可能重新上升，外推会得出「大注更差」的错误结论。
   同样成本下，**1/3、2/3、1.2** 覆盖小/中/大，1/2 和 1.0 都是内插。

**分期应该按牌面，不按尺度**：牌面类是相互独立的任务，加牌面不用重跑已有的；
加尺度必须全量重跑。所以正确顺序是：**先定死最终尺度集合 → 用 20–30 个牌面跑通
全链路 → 按牌面扩到 184 类**。

| 路线 | 首次成本 | 之后 |
|---|---|---|
| 3 档（1/3、2/3、1.2）× 184×3 | 46–101 h | 要上 5 档：全量重跑 230–460 h |
| 5 档 × 30 牌面 × 2 场景（先跑通） | 25–50 h | 按牌面扩到 184×3：再加 205–410 h |
| 5 档 × 184×3 一次到位 | 230–460 h | — |

第一行是「现在最便宜」，但如果以后一定上 5 档，总账（46–101 + 230–460 h）会比
一次到位贵 46–101 h。**所以 v1 必须是「你愿意长期用」的表，而不是临时脚手架**：
184 类 × 3 档本身就是一个可交付产品（1/2、1.0 用内插），即使永远不升级也不亏。

---

## 7. 跑批架构

> **M0 已实现**（2026-10-26）：`gto/runner`（Rust）+ `gto/scripts`（gen/run/ingest）。
> PC 上的安装、命令、断电续跑见 [gto/README.md](../gto/README.md)。下面是设计说明；
> M0 的落盘格式用 gzip JSON（优先可读/好排错），M2 再换二进制。

```
gto/
├── vendor/postflop-solver/        # 已有
├── runner/                        # 新增：Rust bin（约 400 行）
│   ├── Cargo.toml                 # 依赖 ../vendor/postflop-solver
│   └── src/main.rs                # 读 job.json → 建树 → 求解 → 抽节点 → 写 .gto
├── specs/
│   ├── spots.json                 # 场景定义（范围、底池、筹码、抽水）
│   ├── ranges/                    # gto / app 两套 169 键范围
│   └── boards/flop_classes.json   # 184 个代表牌面 + 1,755→184 映射
├── jobs/                          # 由脚本生成的单次任务（可断点续跑）
├── data/
│   ├── raw/                       # 每次求解的 .gto（zstd）
│   └── gto.sqlite                 # 查询库（最终产物）
└── scripts/
    ├── gen_jobs.py                # 生成任务、分片、跳过已完成
    ├── ingest.py                  # .gto → sqlite（含范围统计）
    └── calibrate.py               # 用 3 个牌面测 it/s，外推总时长
```

**runner 的命令行**：

```bash
gto-runner \
  --job jobs/SRP_BTN_vs_BB/Kd9d4c.json \
  --out data/raw/SRP_BTN_vs_BB/Kd9d4c.gto.zst \
  --threads 12 --iterations 250 --target 0.4 \
  --extract flop_root,flop_vs_bet,turn_all,range_stats
```

**任务 JSON**（一文件一次求解，天然可断点续跑、可并行）：

```jsonc
{
  "id": "SRP_BTN_vs_BB__Kd9d4c__v1",
  "flop": "Kd9d4c",
  "oop_range": "BB 防守范围字符串",
  "ip_range": "BTN 开池范围字符串",
  "starting_pot": 12.5,
  "effective_stack": 94.0,
  "bet_sizes": { "flop": ["33%","50%","67%","100%","120%"], "turn": ["67%","120%"], "river": ["67%","120%"] },
  "raise_sizes": ["33%","50%","67%","100%","120%"],
  "iterations": 250
}
```

**跑批策略**：

- 12 核跑 **2 个 6 线程任务**，比 1 个 12 线程任务吞吐略高，且单任务内存减半；
- 每个任务写完 `.gto.zst` 再写一个 `.done` 标记；`gen_jobs.py` 只补缺失的；
- 失败自动重试 2 次，仍失败写 `.failed` 并继续，不阻塞整批；
- 日志每 10 次迭代打一行 `iter, exploitability, ev, elapsed`，方便算 it/s。

### 7.5 跨机器跑批：Mac 跑一部分，PC 接着跑

可以，而且推荐把 Mac 当「第二台 worker」——但必须按 **job 分片**，不能按迭代分片。

**唯一正确的切法**：一个 job = 一个（牌面 × 场景）。每台机器独立跑完整 job，产出
`.gto.zst` + `.meta.json`；最后在一台机器上统一 ingest。不要把同一个 job 的迭代
拆到两台机器——CFR 的状态是连续的，拆了没法合并。

**数据一样吗？**

| 层面 | 是否一致 | 说明 |
|---|---|---|
| SQLite schema / 行结构 | 完全一致 | 同一个 `ingest.py` + 同一个 `format_version` |
| ingest 结果（同一份 `.gto`） | 完全一致 | ingest 是确定性的，跟在哪台机器跑无关 |
| 求解数值（同一 job 两台各跑一次） | 不完全一致 | 线程数不同 → 浮点归约顺序不同；ARM vs x86 → FMA/SIMD 不同。差异在 1e-6~1e-4 |
| 最终策略 | 语义一致 | 184 类 + 尺度近似的误差远大于浮点噪声 |

所以：**每块牌面只由一台机器产出**，就不存在「同一行两个版本」的问题；跨机器的
差异只是浮点噪声，不会让表「一半准一半不准」。

**必须钉死的元数据**（写进 `.meta.json` 和 `solves` 表）：

- `engine_version`（postflop-solver commit）
- `runner_version`（git commit + `Cargo.lock` 哈希）
- `format_version`（`.gto` 文件格式）
- `config_hash`（范围字符串 + 底池 + 筹码 + 尺度 + rake + 迭代数的 sha256）
- `iterations`（**固定迭代数，不要用 target 停止**，否则两台机器停在不同迭代数）
- `exploitability`、`threads`、`source_host`、`created_at_utc`

`ingest.py` 遇到 `format_version` 或 `config_hash` 不一致直接拒绝，避免半新半旧的
数据混进库。

**操作流程**：

1. `gen_jobs.py` 生成 manifest，按 `hash(job_id)` 确定性地分给 mac / pc（比如 25/75）；
2. 两台机器各自 `gto-runner --shard mac|pc`，写 `.gto.zst` + `.meta.json` + `.done`；
3. Mac 的文件 rsync 到 PC（5–20 MB/解，552 解约 3–11 GB）；
4. 只在 PC 上跑一次 `ingest.py`（单写者，避免 SQLite 锁和合并）；
5. `nodes` 表按 `(solve_id, ...)` 唯一，重复 job 用 `INSERT OR IGNORE`，天然幂等。

**Mac 的现实问题**：这台是 MacBook Air M2（4P+4E、16 GB、无风扇）。

- 持续满载会降频，长时间跑的实际吞吐大概只有 5900X 的 **1/3**；
- 16 GB 只够单开一个 job；
- 按 1/3 吞吐分 25% 的活，整体墙钟只快 ~25%（100 h → 75 h），代价是笔记本连跑几天。

所以建议：**Mac 只做消融、校准、20–30 个牌面的试点**，全量交给 PC。真要并行，
给 Mac 分 20–25%，垫高散热、插电、`--threads 6`。

**跨平台格式**：`.gto` 用固定宽度小端（`i32/f32`），不要序列化 `usize`；路径用
`pathlib` + ASCII job id；时间存 UTC。ingest 只读文件，不依赖运行路径。

### 7.6 断电会丢什么

**粒度是「一个 job」**：一个（牌面 × 场景）跑完才落盘。停电最多丢**当前正在跑的那一个**
（3 档 5–11 分钟，5 档 25–50 分钟），已完成的 `.done` 全部保留。

写入顺序（runner 里必须按这个来）：

```
求解完成
  → 写 <job>.gto.tmp
  → flush + fsync（Windows: FlushFileBuffers）
  → rename 成 <job>.gto          # 原子替换
  → 写 <job>.meta.json.tmp → fsync → rename
  → 最后写 <job>.done            # 内含 .gto 的 sha256
```

- 停电时 `.tmp` 不完整，重启后被忽略；
- `.done` 不存在 = 没完成，重跑；
- `.done` 存在但 sha256 对不上 = 文件坏了，重跑；
- **顺序不能反**：先 `.done` 后数据，会出现「标记完成但文件是半截」。

**续跑**：同一条命令再跑一次，`--skip-done` 校验 `.done` 后跳过。不需要记「跑到哪了」。

**SQLite 不是源数据**：`.gto` 才是。ingest 坏了就删库重灌；ingest 本身用单事务
（失败回滚）+ 写到 `gto.sqlite.tmp` 再 rename，避免半截库。

**真正的风险不是停电，是磁盘坏/误删**：`.gto` 重算要几小时到几天。建议每天
`robocopy` / `rsync` 到另一块盘或 NAS（3–11 GB，很便宜）。

**Windows 的坑**：`std::fs::rename` 在目标已存在时会失败（POSIX 是覆盖）。
runner 要用 `MoveFileExW(MOVEFILE_REPLACE_EXISTING)` 或先删再 rename，且删和 rename
之间断电会丢旧文件——所以更好的做法是「新文件名带时间戳/序号，`.done` 指向它」，
或者用 `ReplaceFileW`。

单个 job 很长时（5 档 25–50 分钟），可以每 50 次迭代存一次 checkpoint（solver 支持
序列化），停电最多丢 50 次迭代。第一版可以不做。

---

## 8. 数据 schema（SQLite，单文件）

```sql
-- 牌面与分类
CREATE TABLE boards (
  board_id      INTEGER PRIMARY KEY,
  flop          TEXT NOT NULL,          -- 规范代表，如 'Kd9d4c'
  class_id      INTEGER NOT NULL,       -- 0..183
  features      TEXT NOT NULL           -- JSON：paired/suit/gaps/high/broadway
);
CREATE TABLE board_map (                 -- 1,755 个同构翻牌 → 代表
  canonical     TEXT PRIMARY KEY,
  rep_board     TEXT NOT NULL,
  class_id      INTEGER NOT NULL
);

-- 场景
CREATE TABLE spots (
  spot_id       TEXT PRIMARY KEY,       -- 'SRP_BTN_vs_BB_100bb_app'
  pot_type      TEXT, hero_pos TEXT, villain_pos TEXT,
  starting_pot  REAL, effective_stack REAL, spr REAL,
  range_profile TEXT,                   -- 'app' | 'gto'
  config        TEXT                    -- JSON：尺度、抽水、迭代
);

-- 一次求解
CREATE TABLE solves (
  solve_id      INTEGER PRIMARY KEY,
  board_id      INTEGER, spot_id TEXT,
  iterations    INTEGER, exploitability REAL,
  engine        TEXT, engine_version TEXT, created_at TEXT,
  UNIQUE(board_id, spot_id)
);

-- 节点策略（核心表）：一个节点一行，1,326 手打成 blob
-- 不要一行一手一动作：552 次求解 × 300 节点 × 1,326 手 × 6 动作 = 13 亿行，SQLite 撑不住
CREATE TABLE nodes (
  solve_id   INTEGER,
  street     TEXT,                   -- flop|turn|river
  turn_card  TEXT,                   -- 转牌，flop 时为空
  node_key   TEXT,                   -- 'root' | 'vs_b33' | 'vs_b120' | 'xx' | 'b67c'
  actor      INTEGER,                -- 0=OOP 1=IP
  actions    TEXT,                   -- JSON，决定 blob 的列顺序
  freq       BLOB,                   -- 1,326 × n_actions，int8（百分比 0..100）
  ev         BLOB,                   -- 1,326 × n_actions，int16（毫 BB）
  PRIMARY KEY (solve_id, street, turn_card, node_key, actor)
);
CREATE TABLE hands (                 -- 固定 1,326 组合顺序，全库共用一份
  idx        INTEGER PRIMARY KEY,
  combo      TEXT NOT NULL           -- 'AhKh'
);

-- 范围级统计（给模型看「牌面结构」用，不用扫 nodes）
CREATE TABLE range_stats (
  solve_id      INTEGER, street TEXT, turn_card TEXT, node_key TEXT,
  actor         INTEGER,
  bet_pct REAL, raise_pct REAL, check_pct REAL, fold_pct REAL,
  equity_pct REAL,        -- 该范围对对方范围的权益
  nut_pct REAL,           -- 坚果组合占比（用于坚果优势）
  size_mix      TEXT,     -- JSON {"33":0.46,"50":0.12,...}
  PRIMARY KEY (solve_id, street, turn_card, node_key, actor)
);

CREATE TABLE turn_buckets (
  solve_id INTEGER, turn_card TEXT, bucket TEXT, stats_json TEXT
);
```

**量化**：频率用 `0..100` 的整数（int8），EV 用「毫 BB」整数（int16）。
1,326 手 × 6 动作 × 3 字节 ≈ 24 KB/节点；184 类 × 3 场景 × ~300 节点（翻牌 6 个 +
转牌 49×6 个）≈ 16.5 万行 ≈ **4 GB**。SQLite 单文件、只读、mmap，随机查询 < 1 ms。

---

## 9. 接入 AI 建议

### 9.1 数据流

```
HandSnapshot ──► build_situation() ──► Situation
                                          │
                                          ├─ ground_truth（已有）
                                          └─ gto = gto_lookup.lookup(sit)   ← 新增，同步 <10ms
                                                   │
                     render_hand_facts() ──────────┴─► "## GTO baseline" 段
                                                   │
                                            build_messages() → 模型
```

### 9.2 查询模块骨架

```python
# poker_ocr/gto_lookup.py
@dataclass(frozen=True)
class HandGTO:
    hand: str                      # 'AhKh'
    freqs: dict[str, float]        # 只给解过的档：{'1/3': .55, '2/3': .35, 'check': .10}
    ev: dict[str, float]           # {'check': 11.8, '1/3': 12.6, '2/3': 12.4, '1.2': 10.7}
    interpolated: dict[str, float] # 只给 EV、不给频率：{'1/2': 12.5, '1.0': 11.6}
    best: str                      # '1/3'
    in_range: bool                 # 手牌是否在建模范围里

@dataclass(frozen=True)
class GTOLookup:
    spot: str                      # 'SRP_BTN_vs_BB_100bb_app'
    board_class: int
    class_label: str               # 'K-high two-tone connected'
    confidence: str                # 'exact' | 'class' | 'out_of_range'
    node: str                      # 'root' | 'vs_b33' | 'xx'
    hand: HandGTO | None
    range_stats: dict              # bet_pct / equity / nut_pct / size_mix
    source: str                    # 'gto-v1/184/2026-10-25'

def lookup(sit: Situation) -> GTOLookup | None:
    # 硬门槛：翻后、单挑、牌面/手牌完整、场景命中
    if sit.street == PREFLOP or sit.opponents_in_hand != 1:
        return None
    if not sit.snap.hero_cards or len(sit.snap.board) < 3:
        return None
    key = _spot_key(sit)           # pot_type + 位置 + SPR 档 + range_profile
    if key is None:
        return None
    ...
```

**不命中的情况全部返回 `None`，不要猜**：多人池、短码、SPR 超出档位、手牌不在范围、
抽水版本缺失。宁可没有这段，也不要给一个错的锚。

### 9.3 注入的事实块（渲染示例）

```text
## GTO baseline (approx table v1; heads-up, 100bb, app ranges)

Board class: K-high two-tone connected (rep Kd9d4c; your Ks9s4h is suit-isomorphic → EXACT)
Spot: single-raised pot, BTN (you, PFR, in position) vs BB｜SPR 7.5

Range structure (BTN vs BB):
  equity 58.2% vs 41.8%｜nut combos 14.0% vs 6.0% → BTN has range AND nut advantage
  equilibrium c-bet 71%: 1/3 52%｜2/3 15%｜1.2 4%｜check 29%
  the 1.2-pot bets come from {KK, 99, 44, K9s, QdJd} — polarised, not top pair

Your hand AhKh (top pair, top kicker + backdoor flush):
  EV (BB): check 11.8｜1/3 12.6｜2/3 12.4｜1.2 10.7
  interpolated (approx): 1/2 ≈ 12.5｜1.0 ≈ 11.6
  strategy: 1/3 55%｜2/3 35%｜check 10%
  highest-EV size: 1/3 (+0.8BB over checking)

Read this as: this board is good for the preflop raiser, but your specific hand
prefers the small size; the overbet is reserved for the top of the range.
```

> 上面是**示意数字**，真实值以查询结果为准；渲染函数只允许输出表里的数，
> 不允许自己算或补。
>
> M1 实际渲染的是：范围混合（check/各尺度百分比）、手牌各档频率与 EV、插值 EV
> （标 `[approx]`）、near-flat 提示、来源版本。**权益/坚果优势还没有输出**——
> 那需要在 ingest 时按范围聚合（M2）。

这段的价值不在「告诉你选 1/3」，而在**把牌面结构量化**：权益差、坚果差、
尺度组合、哪些牌用大尺度。模型随后可以自己解释「为什么小尺度」。

### 9.4 代码改动清单（很小）

> **已实现（2026-10-26）**：`poker_ocr/gto_lookup.py` + `Situation.gto` + 渲染 +
> 提示词优先级；测试 `tests/test_gto_lookup.py`、`tests/test_advice.py` 通过。
> 细节见 [gto/README.md](../gto/README.md) §10。

| 文件 | 改动 |
|---|---|
| `poker_ocr/gto_lookup.py` | 新增：查询 + 渲染（约 200 行） |
| `poker_ocr/advice.py` | `Situation` 加 `gto: GTOLookup \| None`；`build_situation()` 里调 `lookup()`；`render_hand_facts()` 里渲染 |
| `poker_ocr/advice.py` | system 提示加一段「GTO baseline 的优先级」 |
| `poker_ocr/live.py` | 无需改（建议是后台线程，查询是同步 10ms） |
| `.env` | `GTO_TABLE_PATH`、`GTO_ENABLED=1`（灰度开关） |

**优先级必须写死**（否则模型会在「GTO 说 check」和「价值优先说 bet」之间摇摆）：

```
硬事实（赔率/全下/多人池） > 桌面动态与画像 > GTO baseline > 通用价值优先规则
```

并且加一句：「如果你选的档位不是 GTO 的 EV 最高档，理由里必须点名
是哪个剥削性理由（对手类型/桌面动态），不能只说『我觉得』。」

### 9.5 选项金额 ↔ 尺度映射

App 的 `preset_targets()` 已经算出五档金额，`option_id` 就是 `raise-1/3` 等。
查询时用**比例**匹配：`option.amount / pot`（面对下注时用 `(amount - max_bet) / (pot + to_call)`）
落到 `{1/3, 1/2, 2/3, 1, 1.2}`，误差 > 0.05 就不匹配（说明被 `_cap()`
抬到最小加注或封顶成全下，此时 GTO 尺度不适用）。

解过的档只有 `{1/3, 2/3, 1.2}`：命中这三档时查节点；1/2、1.0 不查节点，
用左右相邻档的 EV 做二次插值，只给 EV、不给频率，并标 `approx`。

这一步要有单测，覆盖三种情况：正常、被最小加注抬高、后手不足变全下。

### 9.6 多路池怎么办

现场 9 人桌大量是 3–5 人池，求解器不覆盖。三种选择：

1. **第一版直接不注入**（`opponents_in_hand != 1 → None`）。诚实，且现有启发式
   对多路池本来就更准（「多路听牌别诈唬」这类规则 GTO 表给不了）。
2. 第二版做「多路代理」：用 `coach.beat_rate` 的多人版 + 范围权益，给
   「你的手牌 vs N 个对手范围的权益」，只注入权益和坚果率，不注入尺度。
3. 不做多人 GTO（正确，但成本极高，且现场范围噪声太大）。

建议先 1，跑通单挑再评估 2。

### 9.7 多人池的补充层：牌面权益（已实现）

求解器不覆盖 3–5 人池，但「这个牌面对谁有利」本身可以**不算策略、直接算权益**：

- `poker_ocr/board_equity.py` 对**实际牌面**做 Monte Carlo（600 次，~0.1 s），
  用 App 的范围，算 PFR 对 1/2/3 个跟注者的范围权益，以及两对+/顺子+ 的密度；
- 3–5 人池、任何 SPR 都能用，不依赖求解器表；
- 渲染成 `## Board structure`，和 `## GTO baseline` 并列。

这不是「多人 GTO」——它只回答范围优势/坚果优势/人数效应，不回答「下多大」。
多人池的下注尺度继续用现有启发式；单挑 solver 的 c-bet 频率只当参考。

对 3–5 人松浪桌，这一层的边际价值高于把 184 类 × 5 档跑完：
**牌面结构先对了，尺度错一点不至于灾难；牌面读反了，尺度再准也没用。**

---

## 10. 验证与评测

**求解正确性（离线）**

- 每批抽 5 个解，看 `exploitability ≤ 0.5%` 底池；
- 同构不变性：`Kd9d4c` 与 `Ks9s4h` 的解在花色置换后，手牌策略应完全一致；
- EV 一致性：`check` 的 EV 应等于「过牌后对方下注/过牌」的期望（用 `play()` 复核）；
- 交叉验证：3 个牌面用 TexasSolver 重跑，根节点 bet% 差异 < 3 个百分点。

**策略正确性（人工）**

- 选 10 个典型牌面（A 高干燥、K 高两同花、连张、公对、单花、低牌面），
  和 GTO Wizard / Pio 的公开解对比 c-bet 频率与尺度组合，MAE < 5 个百分点。

**产品效果（最有说服力）**

- 用 `logs/live.states.jsonl` 和 `hands_review.csv` 回放历史手牌：
  - 指标 1：所选档位的 EV 损失（相对表内最高 EV，单位 BB/decision）；
  - 指标 2：「尺度错误」次数（选了 EV 低 0.5BB 以上的档）；
  - 指标 3：与现有建议的一致率（预期 60–75%，差异大的手要人工看）。
- 上线后 50/50 A/B（`GTO_ENABLED` 随机），比较每百手盈亏与决策 EV 损失。

**单测清单**

```
test_lookup_hu_only           多人池返回 None
test_lookup_out_of_range      72o 在 BTN 范围外 → in_range=False，仍给 range_stats
test_suit_isomorphism         Ks9s4h 与 Kd9d4c 映射到同一 solve
test_option_mapping           五档金额 ↔ 比例，含 _cap 抬高/全下
test_render_no_hallucination  渲染块里的数字必须来自查询结果
test_spr_bucket               7.5 → 命中 100bb 档；0.6 → 命中 4BP 档或 None
```

---

## 11. 5900X 要跑多久

### 11.1 估算模型

以 TexasSolver 官方 benchmark 为锚（6 线程、200 次迭代、0.275% 底池、
翻牌/转牌各 1 档 + 全下、河牌 2 档、1.6 GB）：**172 秒**。

```
单次墙钟 ≈ 172s
          × (5900X / 6线程 的吞吐比 ≈ 1.7–2.0)      → 86–101s
          × (我们的树 / 基准树 的节点比 ≈ 1.5–2.5)   → 130–250s
          × (250/200 次迭代)                        → 160–310s
          + 抽取/压缩/IO ≈ 20–40s                   → 180–350s
```

→ **翻 5 / 转 2 / 河 2 ≈ 3–6 分钟**（保守按 **4–8 分钟**做预算，含失败重试）。
转河换成 3 档再乘 ≈2.25 → **8–18 分钟**；5 档再乘 ≈6.25 → **25–50 分钟**。

M0 实测（MacBook Air M2、591 手、3 档、加注只留全下）：100 次迭代 291 s、
exploitability 2.83% 底池、2.07 MB/解。按此推算 5900X 12 线程 250 次约 6–10 分钟，
与上面的预算同量级；**最终以 PC 上 1 个 job 的校准为准**。

> 这个数的不确定性主要来自「节点比」。**不要信这个数，先花 20 分钟校准**：
> 挑 3 个代表牌面（A 高干燥 / K 高两同花连张 / 公对），各跑 50 次迭代，
> 用日志里的 `elapsed / iterations` 得到 s/iter，再乘 250。
> `scripts/calibrate.py` 会把结果写成一张「牌面类 × 分钟」的表。

### 11.2 总时长

| 档位 | 牌面 | 场景 | 求解次数 | 转河 2 档（省钱） | 转河 3 档（推荐） |
|---|---|---|---|---|---|
| **MVP** | 60 | 2 | 120 | 8–16 小时 | **16–36 小时** |
| **标准** | 184 | 3 | 552 | 37–74 小时（2–4 天） | **74–166 小时**（3–7 天） |
| **完整** | 184 | 5 | 920 | 61–123 小时 | **123–276 小时**（5–11 天） |
| 全牌面 | 1,755 | 1 | 1,755 | 117–234 小时 | 234–527 小时（10–22 天） |
| 全牌面+3 场景 | 1,755 | 3 | 5,265 | 351–702 小时 | 702–1,579 小时 |

（已含 15% 失败/重跑余量；转牌/河牌的策略从同一棵树抽取，**不额外增加求解时间**，
只增加 10–20% 的抽取与写盘时间。最后一列是 §6.5 的推荐配置；2 档那一列只在消融
证明「够用」时才采用，否则按 3 档报。）

### 11.3 实际感受

- **CPU**：12 核全核 4.1–4.3 GHz，功耗约 140–160W，普通双塔风冷能压住；
  连续跑 3 天建议机箱风道做好，或 `--threads 10` 降 5% 速度换温度。
- **内存**：M0 实测 **3.5–4 GB/任务**（591 手、3 档、压缩）；**32 GB 可以双开**，
  16 GB 只能单开。五档会 ×5，3 档加注会 ×3。
  双开时每个任务 6 线程，总吞吐约等于单开 12 线程，但内存翻倍、单任务更快出结果。
- **磁盘**：原始 `.gto` 每解 5–20 MB（zstd），标准档约 10 GB；SQLite 约 4 GB。
- **电费**：0.15 kW × 74 h ≈ 11 度，忽略不计。
- **中断**：断点续跑是刚需（`gen_jobs.py` 按 `.done` 跳过），否则一次重启白跑。

### 11.4 如果想更快

| 手段 | 收益 | 代价 |
|---|---|---|
| 迭代 250 → 150 | 时间 ×0.6 | 误差 0.4% → 0.6% 底池 |
| 翻牌只留 3 档（1/3、2/3、1.2） | 时间 ×0.6 | 少了 1/2 和 1 的 EV 对比 |
| 转牌/河牌只留 1 档 | 时间 ×0.8 | 转河策略变粗 |
| 牌面类 184 → 100 | 时间 ×0.55 | 类内误差变大，需抽检 |
| 租 32–64 vCPU 云机 | 3–5 天 → 1 天 | 约 $100–300；注意 AGPL 与数据合规 |

### 11.5 换成 RTX 4090 呢

先说结论：**大约快 2.5–4 倍，不是 10 倍**。标准表（3 档、184×3）从 3–7 天变成 **1–3 天**；
5 档全街从 10–19 天变成 **2.5–8 天**。

依据：TexasSolver GPU 作者给的跑分（Qs Jh 2h，<0.2% exploitability）是
**24.4 秒 vs PioSOLVER CPU 86.1 秒**，约 3.5–4 倍（<https://global.v2ex.co/t/1203106>）。
但这里有两个折扣：

1. Pio 不是我们的引擎。`postflop-solver` 官方声称比 Pio 快，所以 4090 相对
   **我们这条 CPU 管线**的净提升更可能是 **2.5–3 倍**，而不是 4 倍。
2. CFR 翻后求解是「树遍历 + 小矩阵」，分支不规则、kernel 启动开销占比高，
   不是矩阵乘法那种能跑满 4090 的负载。树越大（5 档、多街）GPU 利用率越高，
   河边小树反而可能只有 1.5–2 倍。

| 配置 | 5900X | 4090 保守 2.5× | 4090 乐观 4× |
|---|---|---|---|
| 2 档 | 37–74 h | 15–30 h | 9–19 h |
| **3 档（推荐）** | **74–166 h** | **30–66 h** | **19–42 h** |
| 5 档 | 230–460 h | 92–184 h | 58–115 h |

**比时间更需要注意的是工程代价**：TexasSolver GPU 目前是

- **闭源**（仓库只有二进制 + EULA，`gpu_solver` 源码不公开），不是原来说的「开源求解器」；
- **Windows only**，以 GUI 为主，没有我们需要的 CLI/库 API；
- 批量求解能导出 JSON，但要自己写解析 + 入库，格式随版本变的风险高。

所以它**不能直接替换 `gto/runner`**。三条路：

1. **CPU 跑全量、GPU 只做实验**（推荐）：消融、调档、试新 spot 用 GPU GUI 手动跑，
   确认参数后再用 CPU 管线自动跑全量。工程零改动。
2. **GPU 出数据、CPU 入库**：用它的批量求解导出 JSON，写一个 `ingest_gpu.py` 解析。
   省时间，但要跟着它的格式走，且 license 是 EULA（自用可以，分发不行）。
3. **自己写 CUDA 版**：工作量以月计，收益 2–4 倍，不建议。

如果只是「跑一次出表」，CPU 多等 2–4 天比接一套闭源 GUI 便宜。如果要做很多次
消融/迭代（大概率会），4090 值——但值在**缩短实验循环**，不在「一夜跑完全量」。

电费：4090 满载 ~350W，60 小时约 21 度，忽略不计。

---

## 12. 里程碑

| 阶段 | 内容 | 产出 | 工时 |
|---|---|---|---|
| **M0** 打通 ✅ | Rust runner + 3 个脚本 + 2 个 spot × 48 牌面；试点 100 次迭代验证 | `gto/runner` + `gto/scripts` + [README](../gto/README.md) | 2026-10-26 完成 |
| **M1** 查询层 ✅ | `gto_lookup` + `Situation.gto` + `## GTO baseline` + 测试 | 已接入，待真实表验证 | 2026-10-26 |
| **M2** 标准表 | 184 牌面 × 3 spot；转牌全存；SQLite 上线 | 4 GB 查询库 | 后台跑 3–7 天 |
| **M3** 接入打磨 | 优先级规则、灰度开关、监控、失败回退 | 线上灰度 | 1–2 天 |
| **M4** 扩展 | 抽水版、4BP、多人代理、按需转河求解 | 迭代 | 按需 |

**建议顺序**：M0 的校准结果出来再决定 M2 的规模。如果实测是 8 分钟/次，
就先做 184×2（25–50 小时），别一上来 920 次。

---

## 13. 风险与坑

| 风险 | 影响 | 对策 |
|---|---|---|
| 多人池占多数，表用不上 | 价值打折 | 先只做单挑；多人池明确 `None`，并在复盘里统计覆盖率 |
| 转河档位少，1/2 的防守节点缺失 | 高频响应只能插值 | 默认 3 档 + `approx` 标注；1/2 多就上 4 档或精细层 |
| 范围口径不一致 | 表说弃、App 说打 | 用 `app` 范围跑主表，`gto` 范围做对照 |
| 6BB 开池导致 SPR 与通用解不同 | 尺度/打光判断错 | 底池按 12.5BB 配，见 §4 |
| 模型盲信 GTO | 剥削性打法退化 | 优先级写死 + `confidence` 标注 + A/B |
| `_cap()` 把档位抬到最小加注/全下 | 尺度对不上 | 映射误差 > 0.05 时不给 EV 对比 |
| 抽水没建模 | 薄价值过松 | 第二版单独出 `_rake` 库 |
| AGPL | 分发受限 | 自用；商业化前换引擎 |
| GPU 版闭源、Windows only | 接不进自动管线 | 只用来做实验；全量仍走 CPU |
| 5900X 连跑 3 天 | 过热/中断 | 断点续跑 + 温度监控 + 夜间跑 |
| 184 类不够细 | 边缘牌面策略偏 | 抽样验证；必要时加到 250–300 类 |

---

## 14. 需要你拍板的三件事

1. **主表先覆盖哪些 spot？** 默认：`BTN vs BB`、`CO vs BB`（SRP）+ `BB 3-bet vs BTN`。
2. **内存多大？** 16 GB 只能单开（总时长 +0–10%，但更稳）；32 GB 可双开。
3. **要不要抽水版？** 默认先无抽水跑通，第二版再加 `rake 5% cap 3BB`。
4. **转河几档？** 默认 3 档（1/3、2/3、1.2），比 2 档多约 2 倍时间；也可以用 M0 的
   消融结果来决定。5 档全街不建议。

这三点确定后，M0 当天就能开始跑校准。

---

## 15. 全量目标：5 档 × 184 类 × 4 街 × 全位置

这是最终形态，不是一次能跑完的。先把账算清楚，再决定分期。

### 15.1 翻前是另一个引擎

`postflop-solver` 从翻牌开始，翻前范围是**输入**。要「包括翻前」，需要单独的
翻前求解器（169 手 × 位置 × 动作树的 CFR），或者一张按位置/动作/筹码深度组织的
翻前范围表。9-max 全位置、五档加注的翻前均衡树，本身就是一个独立项目
（状态空间随位置数指数增长）。

务实做法：**翻前用「位置 × 动作 × 深度」的范围表**（RFI / 3-bet / 冷跟 / 4-bet），
不追求逐手混合频率；翻后仍用现在的求解器。两者在数据层用同一个 `spot_id` 拼起来。

### 15.2 位置 → spot 家族

9-max 的非盲位置有 7 个。全位置两两组合是 56 对，但真正高频的只有：

| 家族 | 数量 | 说明 |
|---|---|---|
| SRP：7 个开池位 vs BB 跟注 | 7 | 已生成（`spots.json`） |
| 3BP：BB 3-bet vs 7 个开池位 | 7 | 开池方跟注，BB 无位置 |
| 4BP：7 个开池位 4-bet vs BB | 7 | SPR < 1，翻牌基本全下 |
| 溜入：SB vs BB | 1 | SPR 极高，尺度另算 |
| 冷跟：IP 位跟注前位开池 | 5–10 | 范围窄，优先级低 |

核心 14 个（7 SRP + 7 3BP）已经能覆盖现场绝大多数大池；全量 22–30 个是「完整」。
**位置是独立 job，可以后加，不会作废已跑的数据。**

### 15.3 5 档的真实成本

实测（180 手、5 档、加注只留全下）：压缩内存 **4.87 GB**，1 次迭代 11.5 s（4 线程）。
按手牌数线性外推，686 手的 5 档树约 **18–19 GB 压缩内存**、250 次迭代
**15–25 分钟/job**（5900X 12 线程）。

| 规模 | 求解次数 | 墙钟（20 min/job） | 内存 |
|---|---|---|---|
| 184 × 2 spot | 368 | **5–6 天** | 19 GB/任务 |
| 184 × 7 spot（全 SRP） | 1,288 | **18–22 天** | 同上 |
| 184 × 14 spot（+3BP） | 2,576 | **36–45 天** | 同上 |
| 184 × 20 spot（+4BP/溜入） | 3,680 | **51–64 天** | 同上 |

- 32 GB 只能单开（19 GB + 系统）；**64 GB 才能双开**。16 GB 的 Mac 跑不了 5 档全范围。
- 转河各 5 档时，内存/时间是 2 档的 ≈6.25 倍；上面已经是 5 档的数。
- 河牌若按「每张牌精确存」，单解 25,872 个 river 节点 ≈ 0.8 GB，368 解 ≈ 300 GB；
  改按「河牌桶 × 169 手牌类」存，单解 ~20 MB，368 解 ~7 GB。

### 15.4 先补河牌抽取，再开跑

现在的 runner **只抽翻牌 + 转牌**；5 档树虽然把河牌算出来了，但没有落盘，
而求解器不保存整棵树（bincode 已关），所以**跑完也查不到河牌**，等于白跑。

在开 5 档全量之前，必须先加：

1. 河牌**分桶抽取**：每个转牌节点 × 8–10 个河牌桶 × 169 手牌类，存频率/EV；
2. `nodes` 表加 `hand_class` 维度（0–168），查询时把 hero 手牌映射到类；
3. 河牌桶规则：完成同花 / 完成顺子 / 公对 / 高张 / 空白，按 reach 加权聚合。

这是 1–2 天的代码 + 测试；**不做这一步，5 档全量的河牌部分不可用**。

### 15.5 分期建议

| 期 | 内容 | 墙钟 | 备注 |
|---|---|---|---|
| P0（现在） | 3 档 × 48 类 × 2 spot，翻牌+转牌 | 1 晚 | 验证链路/接入，已在跑 |
| P1 | 5 档 × 184 类 × 2 spot（BTN/CO vs BB SRP）+ 河牌桶 | 5–6 天 | 需要先补河牌抽取 |
| P2 | + 其余 5 个 SRP 开池位 | +13–16 天 | 位置独立、可增量 |
| P3 | + 7 个 3BP | +18–23 天 | 大池决策 |
| P4 | 4BP / 溜入 / 冷跟 | +15–20 天 | 低频，按需 |
| 翻前 | 位置/动作范围表（独立轨道） | 1–2 周开发 | 不占求解机时 |

单机 5900X 跑完 P1–P4 约 **2–3 个月**；P1+P2（全 SRP）约 **3 周**。
要更快只有两条路：多机分片（job 独立，天然可并行）或云上 64–128 vCPU。

---

## 参考

- TexasSolver 官方 benchmark（6 线程 / 约 200 次迭代 / 0.275% 底池 / 172 s）：
  <https://github.com/bupticybee/TexasSolver>（README 的 Speed benchmark 表）
- postflop-solver（本方案引擎，AGPL-3.0-or-later）：
  <https://github.com/b-inary/postflop-solver>
- 184 种翻牌分类的常见说法：
  <https://zhuanlan.zhihu.com/p/620074061>（仅作分类数量参考，具体聚类自己算）
- TexasSolver GPU 的 4090 跑分（24.4 s vs Pio CPU 86.1 s）：
  <https://global.v2ex.co/t/1203106>；程序页：<https://bupticybee.github.io/texassolver_gpu_page/>

---

### 附：一句话版本

> 用已经 vendor 的 postflop-solver，翻牌放 5 档、转河各 3 档，184 个牌面类 × 3 个
> 单挑场景，5900X 跑 3–7 天得到一张 4 GB 的 SQLite；线上按「同构牌面 + 手牌 +
> 节点」查一次，把权益差、坚果差、五档 EV 和范围尺度组合作为事实注入提示词，
> 让模型从「猜牌面」变成「读数字」。
