# GTO 跑批（M0）

状态：**M0 + M1 查询层可用**（2026-10-26）。链路已在本机（MacBook Air M2）跑通：
`gen_jobs → gto-runner 求解/抽节点 → .gto.gz + .done → ingest → SQLite`。

M0 的目标不是「最终表」，而是让你今晚在 PC 上把数据跑起来、并且断电不丢。
方案与时间估算见 [docs/GTO近似范围表-方案.md](../docs/GTO近似范围表-方案.md)。

---

## 1. PC 环境

- Windows 10/11 x64
- **Visual Studio Build Tools**，勾选「使用 C++ 的桌面开发」（MSVC 链接器）
- **Rust stable**：<https://rustup.rs>，装完 `rustc --version` 能出版本
- **Python 3.10+**：`gen_jobs / run_batch / ingest / query` 只用标准库；
  只有 `build_boards.py` 需要 numpy（PC 上一般不重跑，`boards.json` 已生成）

## 2. 编译

```powershell
cd C:\poker-ocr\gto\runner
cargo build --release
```

产物：`gto\runner\target\release\gto-runner.exe`。

首次编译会拉 crates.io 依赖，几分钟；之后增量编译几秒。

## 3. 生成任务

```powershell
cd C:\poker-ocr

# 试点：12 个牌面 × 2 个 spot，15 次迭代（验证链路，几分钟）
python gto\scripts\gen_jobs.py --spots BTN,CO --limit 12 --iterations 15

# 正式（3 档）：184 个牌面 × 2 个 spot = 368 个 job，250 次迭代
python gto\scripts\gen_jobs.py --spots BTN,CO --sizes three_span --iterations 250

# 5 档：等河牌抽取补丁（见 docs §15.4）；现在跑会丢河牌
# python gto\scripts\gen_jobs.py --spots BTN,CO --sizes five --iterations 250
```

`sizes` 可选：`three_span`（默认，1/3、2/3、1.2）、`three_modal`（1/3、1/2、2/3）、
`five`（五档）。`--spots` 过滤位置（`BTN,CO` / `UTG` / 留空 = 全部 7 个 SRP 位）。

**5 档的内存**：实测 180 手压缩 4.87 GB，外推到 686 手约 **18–19 GB/任务**；
32 GB 只能单开，64 GB 才能双开。16 GB 的 Mac 跑不了 5 档全范围。

## 4. 开始跑

```powershell
# 单任务，12 线程（5900X 用物理核数）
python gto\scripts\run_batch.py --threads 12

# 或双开：每个 6 线程，需要 ~16GB 内存
python gto\scripts\run_batch.py --parallel 2 --threads 6
```

- 前台运行，Ctrl+C 可停；**重新执行同一条命令 = 断点续跑**。
- 已完成的 job 有 `.done`，会被跳过。
- 日志：`gto\logs\batch-*.log`；失败任务写 `<job>.failed`，自动重试 2 次。

先跑试点确认没问题，再跑全量：

```powershell
python gto\scripts\run_batch.py --limit 2 --threads 12
```

**迭代数怎么定**：250 次大约到 **1.1–1.5% 底池**的 exploitability，对近似表够用；
想到 0.5% 以下要 600 次以上（时间 ×2.4）。先按 250 跑，抽 3 个 job 看日志里的
`exploitability`，高于 1.5% 再整体加到 400。

## 5. 入库

```powershell
python gto\scripts\ingest.py
python gto\scripts\ingest.py --stats
```

- 只读 `.done`；`.tmp` / 孤儿文件忽略。
- 幂等：同一 job 重复入库会替换旧节点。
- 库坏了就 `--rebuild` 重灌；`.gto.gz` 才是源数据。

## 6. 断电会丢什么

**最多丢当前正在跑的那一个 job**（250 次迭代约 6–10 分钟）。

写入顺序（runner 已实现）：

```
求解完成 → 写 <job>.<ts>.gto.json.gz.tmp → fsync → rename
        → 写 .meta.json（含 sha256）→ 最后写 .done
```

- 断电时 `.tmp` 是半截，重启后被忽略；
- `.done` 不存在 / sha256 不对 → 重跑；
- 重跑同一条命令即可，不需要记进度。

**真正的风险是磁盘坏或误删**：`.gto.gz` 重算要几小时到几天，建议每天
`robocopy gto\data\raw D:\backup\gto-raw /MIR`。

## 7. 实现说明与 M0 限制

**引擎**：仓库自带的 `postflop-solver`（Rust，AGPL）。做了两处补丁：

1. 关闭 `bincode` feature —— crates.io 上的 bincode 2.0.1 与 vendored 代码的
   `Decode` trait 不兼容（`2.0.0-rc.3` 是旧 API）。M0 不需要存档/读档。
2. `#![allow(dangerous_implicit_autorefs)]` —— Rust 1.99 把这个 lint 升级成
   deny，vendored 代码里有 3 处旧写法。

**已知限制**（M0 有意为之）：

| 限制 | 原因 | 后续 |
|---|---|---|
| 加注只有 all-in（`raise_sizes: "a"`） | 3 档加注把内存从 3.5GB 拉到 12GB+、时间 ×8 | M2 加 1 档 67% |
| 只抽翻牌 + 转牌，不抽河牌 | 河牌 2,352 个 runout，存储 ×100 | 按需转河求解 |
| 牌面 184 个代表（`boards.json`，k=184） | 已生成；48 类试点版可回退 | 无需再聚类 |
| 无抽水 | 先和 GTO 视频对照 | 单独 `_rake` 库 |
| 多人池不覆盖 | 求解器是 HU | 查询层返回 `None` |

**数据口径**：

- 金额单位：chip，1 BB = 100 chip（`starting_pot=1250` = 12.5BB）。
- 迭代数固定（target=0），不用「跑到 0.4% 就停」，保证 Mac/PC 结果可比。
- 频率量化为 0–100，做最大余数修正，每手和恰好 100。
- EV 单位 chip；`fold` 的 EV 记为 0（库的口径），比较「bet vs check」不受影响。

## 8. 实测（MacBook Air M2，4 线程）

| 指标 | 数值 |
|---|---|
| 手牌并集 | 591（BB 防守 + BTN 开池） |
| 节点数 | 596（翻牌 8 + 转牌 588） |
| 内存（压缩） | 3.56 GB |
| 速度 | 2.3–5.3 s/iter（4 线程，会降频） |
| 单解体积 | ~1.9 MB gz |
| 15 次迭代 | ~35 s（空载） |
| 100 次迭代 | **291 s，exploitability 2.83% 底池** |
| 单解体积（591 手） | 2.07 MB gz |

5900X（12 线程）预计 1.2–2.0 s/iter → 250 次迭代 **6–10 分钟/job**；
368 个 job（3 档）约 **21–37 小时**；7 个位置全开约 **3–4 天**。跑之前先用 1 个 job 校准——
这是**唯一可信**的数字来源，Mac 的实测只能当量级参考（无风扇会降频）。

## 9. 目录

```
gto/
├── runner/                  # Rust：求解 + 抽节点 + 原子落盘
│   └── src/main.rs
├── scripts/
│   ├── gen_jobs.py          # specs → jobs + manifest
│   ├── run_batch.py         # 跑批 / 续跑 / 重试
│   ├── ingest.py            # .done + .gto.gz → SQLite
│   └── build_boards.py      # 1755 翻牌 → k 类代表（默认 48，可 184）
├── specs/
│   ├── spots.json           # 2 个 spot + 范围
│   ├── sizes.json           # three_span / three_modal / five
│   ├── boards.json          # 48 个代表牌面
│   └── board_map.json       # 1755 → 类
├── jobs/                    # 生成的 job（gitignore）
├── data/                    # raw + sqlite（gitignore）
└── logs/                    # 跑批日志
```

## 10. M1：接进 AI 建议（已实现）

- `poker_ocr/gto_lookup.py`：查表 + 渲染事实块，同步 <10ms。
- `advice.Situation.gto`：`build_situation()` 末尾查一次；frozen dataclass 用
  `dataclasses.replace` 带出。
- `render_hand_facts()` 在牌力事实后追加 `## GTO baseline`。
- system 提示新增「GTO baseline」优先级：数学/读牌/桌面动态 > GTO > 通用尺度规则。

只在**单挑 + 单加注池 + SPR 5.5–10.5 + 翻牌/转牌**时注入；其余一律 `None`。
牌面用花色同构映射到代表牌面：同一类内标 `[approx]`，代表牌面本身标 `EXACT`。

环境变量：

```
GTO_ENABLED=1                 # 0 = 完全关闭
GTO_TABLE_PATH=gto/data/gto.sqlite
GTO_SPECS_DIR=gto/specs
```

测试：

```powershell
python tests\test_gto_lookup.py
python tests\test_advice.py
```

渲染样例（数字来自 15 次迭代的试点，仅示意）：

```
## GTO baseline (approx table v1; HU, ~100BB, app ranges)
Table: SRP_BTN_vs_BB_100bb_app | class 1/48 | rep 5c8c8d | EXACT (suit-isomorphic)
Node: flop, checked to you (no bet)
Equilibrium range mix: check 14% | 1/3 43% | 2/3 26% | 1.2 17%
AhKh: check 42% (EV +6.03) | 1/3 32% (EV +5.08) | ...
[approx] 1/2 EV +4.58 | 1 EV +3.18 (interpolated; no frequency)
Highest-EV size: check (+6.03BB), next 1/3 (+5.08BB, gap 0.95BB).
```

## 10.5 牌面权益层（多人池，已实现）

求解器是单挑的；你桌上 3–5 人池，直接套 HU 策略会错。补的一层是
`poker_ocr/board_equity.py`：

- 对**实际牌面**现算 Monte Carlo（默认 600 次，约 0.1 s），不套 184 类代表牌面；
- 输出 PFR（按实际开池位）对 1/2/3 个跟注者的范围权益、两对+/顺子+ 密度；
- 3–5 人池也能用，不依赖求解器表；
- 渲染成 `## Board structure`，在 `## GTO baseline` 之前。

样例：

```
## Board structure (range equity, Monte Carlo live)
Board: 5c8c8d | SRP_BTN_vs_BB_100bb_app | 600 samples | class 1/184 (rep 6c9c9d, class context)
PFR (BTN) range equity: 1 caller 53% | 2 callers 37% | 3 callers 29%
Strong hands (two pair+): PFR 40% vs caller 34% | straight+: 3% vs 3%
Read: PFR range/nut advantage; 2 opponent(s) in the pot.
```

离线批量版 `build_equity.py`（7 spot × 184 类 × 800 抽样，约 67 s）用于分析/对照；
线上查询不依赖它，直接现算。

**多人池的使用口径**：只信「范围权益 / 强牌密度 / 人数」这三件事；
`## GTO baseline` 的 c-bet 频率是单挑参考，3 人以上不要当指令。

## 11. 下一步（M2）

**优先级 0（已完成）：牌面权益层**——多人池现在就能用，不需要等求解器全量。

**优先级 1：河牌分桶抽取。** 5 档树已经把河牌算出来了，但现在没落盘，跑完也查不到。
需要：每个转牌节点 × 8–10 个河牌桶 × 169 手牌类，按 reach 加权聚合；`nodes` 加
`hand_class`；查询层把 hero 手牌映射到类。这是开 5 档全量前的硬前提。

2. 5 档 × 184 × 2 spot（BTN/CO vs BB），约 5–6 天。
3. 按位置扩：其余 5 个 SRP 位 → +13–16 天；7 个 3BP → +18–23 天。
4. 转牌线补全（check-bet-call、加注线）。
5. 翻前单独轨道：位置/动作范围表（不是 postflop-solver 能做的）。
6. 回放 `logs/live.states.jsonl`，量「有表 vs 无表」的 EV 损失。
