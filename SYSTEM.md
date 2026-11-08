# SYSTEM.md — poker-ocr 系统地图（给 AI / 新人）

> 目的：让任何 AI 或新同事在 **读一份文件** 后就能上手改这个仓库。
> 维护约定：结构变了就改这里；口径（§6）与提示词版本（§7）是最容易踩坑的部分，优先更新。
>
> 生成依据：CodeGraph 索引（231 文件 / 6166 符号）+ 顶层模块 docstring + 47 篇 `docs/` 设计文档。
> 最近核对：**2026-11-05 工作区**（已含 GTO 移除 + P0/P1 策略表重构；提交基线 `df349b9`）。

---

## 1. 一句话

**把德州扑克牌桌画面变成「可落库的牌局事实 + 可执行的 AI 建议」**：

- 输入：OBS / RTSP 视频帧（或一张截图）
- 中间：OCR → 归一化坐标 → 座位环 → 帧状态机 → 手牌行动线
- 输出：`logs/*.jsonl`、手牌 outbox、MySQL 手牌库、桌面自动点击、复盘控制台

两个可独立运行的服务 + 一组离线工具：

| 服务/工具 | 用途 | 默认地址 / 入口 |
|---|---|---|
| `api.py` | 单图识别 HTTP 服务 | `:8000`，`uvicorn api:app` |
| `poker_ocr.live` | 实时推流 → 建议 → 自动点击 | CLI，`python -m poker_ocr.live` |
| `poker_ocr.live --web` | 实时 Web 看板（只读：牌桌/建议/提示词/帧流） | `http://127.0.0.1:8020` |
| `replay-console/` | 手牌库 + 多版本 A/B 复盘 | `:8010`，独立 FastAPI + MySQL |
| `poker_ocr/tables/` | 策略表（对手范围 / 弃牌率数字的单一来源） | `STRATEGY_TABLES_PROFILE` 切 v0 / v1 |
| `tools/` | 离线分析（画像 / 漏损 / SFT / 回放 / 表格对比） | 单文件 CLI |

---

## 2. 目录地图

```
api.py                  # HTTP 入口：/health /ocr/table /ocr/cards /coach/advice
extract_table.py        # 截图 → 识别结果 JSON（CLI）
scan.py                 # ⚠ 已冻结的旧标定工具（仅保留 clean_number / preprocess / RapidEngine 被 import）
rois.json               # ⚠ 现在是「识别结果」文件，不是配置！几何配置在 config/
config/                 # 牌桌几何 profile（归一化坐标、环序、阈值）+ 点击器坐标 + MediaMTX
poker_ocr/              # 全部核心逻辑（45 个模块 + __init__，见 §4）
  prompt/               # 提示词包：system / user / shared / offline
  assets/               # 点数模板图集（工具生成，勿手改）
  tables/               # 策略表 JSON（26 张表：v0 手调 / v1 推导），改数字改这里
  live_web.py           # 本地 Web 看板（stdlib HTTP + SSE，只读）
  live_log.py           # 事件 JSONL + 控制台文本落盘
  web/live/             # 看板前端（index/app/style，无构建）
replay-console/         # 独立子项目：手牌库 + A/B 复盘控制台（Python 后端 + React 前端）
tests/                  # 51 个测试文件 + golden/ 期望 JSON，run_all.py 分层执行
tools/                  # 离线 CLI（回放 / 画像重建 / 报表 / SFT 数据集 /
                        # hand_recognition_audit.py 手牌识别倒查）
docs/                   # 51 篇设计文档与复盘（业务语义的真正来源）
data/                   # 运行期数据：hand_outbox/、opponents/、sft/
logs/                   # live.states.jsonl、复盘产物、incidents/
images/                 # 标注样本与截图素材
```

**三个最容易搞错的点**：
1. `rois.json` 是**输出**。几何配置在 `config/table_*.json`；`config/*.json` 里的 `reference_size` 只作溯源，不参与运算。
2. `scan.py` **已冻结**。新代码走 `poker_ocr.pipeline`，不要往 `scan.py` 里加逻辑（它的 `clean_number` 仍有静默错值路径，见 §9）。
3. `gto/` 已在 2026-11-05 **整体移除**（求解器、specs、`gto_lookup`、`board_equity` 都没了，别去找）；范围/弃牌率的数字现在只在 `poker_ocr/tables/`。

---

## 3. 架构总览

### 3.1 实时链路（主链路）

```
牌桌 App 画面
   │  手机 → OBS 虚拟摄像头(cam:auto) 或 OBS→SRT→MediaMTX→RTSP
   ▼
poker_ocr.live  ── RtspReader 线程（只保留最新帧，覆盖式邮箱）
   │
   ├─ cards.recognize_cards()        纯 OpenCV 读牌（每帧，几毫秒，不走 OCR）
   ▼
StateStream.ingest()                  帧状态机：record / no_change / drop
   │  frame_state.FrameState          ROI 文本 + 解析 → 结构化桌面状态
   ▼
LiveHandTracker                       逐帧累积行动线 → 轮到我时产出「决策点」
   │
   ├─ build_situation()              situation.Situation：模型看到的唯一事实源
   │     ├─ ground_truth            牌型/踢脚/坚果（精确枚举，不可由模型断言）
   │     ├─ range_flow              对手范围事件模型（按动作连乘）
   │     ├─ hand_strength           一行「大/中/小 + 价值/听牌」结论
   │     ├─ fold_equity             逐座位条件弃牌率
   │     ├─ opponent_profile        本手在局对手画像（VPIP/PFR/AF…）
   │     └─ options.build_options() 可选动作 id + 金额（尺度不由模型生成）
   │
   ├─ prompt.build_messages(sit)     system + user 两级提示词
   ▼
AdviceRunner（后台线程，绝不阻塞 OCR 主循环）
   │  OpenAI 兼容接口（AI_SERVER_*）
   ▼
Advice ──┬─ Clicker                 建议落到客户端按钮（macOS CoreGraphics）
         ├─ JsonlWriter             logs/live.states.jsonl（即时 flush）
         ├─ LiveEventLog            logs/live.events.jsonl（帧级诊断索引）
         ├─ LiveWebServer           :8020 看板（SSE，只读）
         └─ HandOutbox              一手一文件 → 异步进手牌库
```

### 3.2 沉淀与复盘链路

```
logs/live.states.jsonl ──► tools/export_live_hands.py ──► hands/*.hands.json
        │
        └─► HandOutbox(data/hand_outbox/pending/*.json)
                 │  HandIngestWorker 批量 POST（失败留在 pending，坏手降级单发）
                 ▼
        replay-console :8010  ──►  MySQL（review_hands 只读 + replay_* 表）
                 │                 │
                 │                 └─► replay/runner.py 重跑：多版本 × N 次重复
                 ▼
        React 控制台（手牌库 / 逐街对比 / 收藏 / 批量重跑 / 导出）
```

### 3.3 策略表与裁判链路（离线）

```
poker_ocr/tables/{range_flow,fold_equity}.json     26 张表，每个数字带 source/verified/needs
        ▲
        │  tools/export_strategy_tables.py   导出 v0 + --check 逐值校验
        │  tools/derive_strategy_tables.py   按 defend model 推导 v1
        │
STRATEGY_TABLES_PROFILE=hand-tuned|sourced
        │
        ▼
range_flow.py / fold_equity.py  ──读表──►  范围似然 · 条件弃牌率 · 尺寸弹性
        │
        ├─ tools/strategy_table_diff.py    v0/v1 在真实决策点上的数值差
        ├─ tools/fold_curve_probe.py       实测聚合弃牌率曲线（价格口径已单测）
        └─ tools/showdown_range_check.py   摊牌反核范围构成 + PIT 校准
```

裁判仍未完成：`P(fold | 手牌档)` 在本地数据里**不可识别**（弃牌看不到手牌），
所以 P2 只能先量「按价格的聚合曲线」，再谈校准。

---

## 4. 模块地图（`poker_ocr/`）

### 4.1 视觉识别（单帧 → 结构化）

| 模块 | 职责 |
|---|---|
| `profile.py` | 加载/校验几何配置；归一化坐标 ↔ 像素；环序与方向断言 |
| `ocr.py` | `OcrBox`（带框结果）+ `OcrEngine`（可选 `det_side_len` 显式缩放，坐标换算回原图） |
| `layout.py` | 文本框 → 槽位分配（几何/类型门，不用内容黑名单） |
| `fields.py` | 筹码解析（**BB 单位闸**）、名字谓词与重试阶梯 |
| `dealer.py` | 庄位检测：`manual` / 白底 D 圆片 blob；找不到就报 `not_found`，不猜 |
| `positions.py` | 庄位 + 环序 → 位置名（SB/BB/UTG/…/BTN） |
| `pipeline.py` | 单帧 → `FrameResult`（每座位带稳定 `seat_id` 与逐字段 score，**可跨帧合并**） |
| `full_table.py` | 备选路线：全图 OCR → 名字/筹码聚类 → 环序 → hero（不假设固定布局） |
| `cards.py` | 公共牌/手牌：颜色 + 形状判花色，字形模板 NCC 判点数（纯 OpenCV） |
| `capture.py` | macOS 屏幕采集 + dhash 抽帧去重（旧「模拟器陪打」第一段） |
| `clicker.py` | 桌面自动点击；矩形内随机点 + 抖动；后端可注入 |

### 4.2 状态与时序

| 模块 | 职责 |
|---|---|
| `frame_state.py` | 一帧完整桌面状态；`errors` 非空 ⇒ 调用方应整帧丢弃（单座 `bet > pot` 例外：降级成 `bet_unknown`）；盲注可继承，底池不可 |
| `state_stream.py` | 防抖状态机：`record` / `no_change` / `drop`；一手内锁定座位身份；只做状态不做 I/O |
| `hand_track.py` | `LiveHandTracker`：逐帧累积行动线，同座同街同动作去重，街末用底池对账补漏 |
| `hand_id.py` | 手牌 ID：名单/位置/日期摘要 + 碰撞后缀 |
| `situation.py` | `Situation`：几何、牌力、范围、画像、桌况、数据缺口——**模型输入的唯一事实源** |

### 4.3 决策与模型

| 模块 | 职责 |
|---|---|
| `advice.py` | `advise()`（一次建议 + 完整 prompt 快照）、`AdviceRunner`（后台、只留最新一个待办） |
| `options.py` | 可选动作空间（翻后按池倍数、翻前按位置/前注/溜入者） |
| `coach.py` | 规则引擎（`advise()` 纯规则版 + 牌型评估原语），被 `ground_truth` / `hand_strength` 复用 |
| `ground_truth.py` | 不可变牌力事实；`beaten_by` / `hero_is_nuts` 用精确枚举 |
| `board_range.py` | 牌面连接度 × 翻前先验 → 动作条件范围（**旧路径**，按街一次过滤） |
| `range_flow.py` | 范围事件模型：按动作顺序连乘，成牌档按牌面百分位（**新路径**） |
| `hand_vs_ranges.py` | 手牌 vs 范围：精确 `behind` + MC 权益 |
| `hand_strength.py` | 纯判定层：`size` / `equity_tier` / `kind` / `summary_en` |
| `fold_equity.py` | 逐座位条件弃牌率与各尺寸盈亏平衡（数字来自 `tables/fold_equity.json`） |
| `strategy_tables.py` | 策略表加载器：profile → 变体，缺变体回落 v0 并标记 `fell_back` |
| `pot_math.py` / `allin.py` / `preflop_ranges.py` / `ranges.py` / `field_ranges.py` | 底池赔率、全下判定、翻前范围与先验 |
| `opponent_profile.py` | 对手画像：按机会计数统计 → 确定性分类；`n<8` 一律 unknown；人工 pin 优先 |
| `table_dynamics.py` | 桌况/桌风描述 |
| `env.py` / `runtime_profile.py` | `.env` 解析、运行时参数 |

### 4.4 沉淀与同步

| 模块 | 职责 |
|---|---|
| `hand_outbox.py` | 一手一文件、原子写、成功才迁 `sent/`、超次进 `failed/` |
| `hand_ingest.py` | outbox → replay-console 的同步 worker（批量优先，坏数据自动降级单发；401/403 不挪文件） |

### 4.5 提示词包（`poker_ocr/prompt/`）

```
system/   输出契约、事实优先级、规则仲裁、决策顺序、指标口径、筹码深度、场景段
user/     当前局面、行动线、翻前、实时牌力、范围、画像、攻击/剥削、数据质量、可选动作
shared/   数字格式与中文词表
offline/  旧英文提示词与策略层（只服务复盘，不进 live）
```

对外只有三个入口：`build_messages(sit)`、`build_system(sit)`、`render_facts_zh(sit)`。
**加一段事实**：在 `user/` 新建模块并在 `user/__init__.py` 组装表注册。
**加一条场景规则**：写进 `system/scenario.py` 的条件函数，不要塞进恒定段。

---

## 5. 数据契约（落盘与接口）

### 5.1 `logs/live.states.jsonl`

一行一个完整 JSON，四种记录：`session_start` / `stream_info` / `state` / `session_end`。
每条立即 flush，硬杀最多丢最后半行。`state` 字段顺序固定：
**个人手牌 → 位置 → 筹码 → 公共牌 → 全局 → 其他对手**（`opponents` 永远最后）。

`--test`（按实战视频演练）写的是 `logs/live.test.jsonl`，并强制不写手牌
outbox：测试手牌不会进手牌库。`session_start` 里会带 `"test_mode": true`。

### 5.1.1 `logs/live.events.jsonl`（排查索引，不是业务契约）

一行一个 `live.event.v1` 事件：`session_start / status / frame / decision / advice_skip /
advice / hand / incident / session_end`。`frame` 每帧一条，带 `hand_id` /
`kind` / `errors` / `cards.warnings` / `state_index`；`advice` 带
`prompt_sha256` / `prompt_chars` / `source` / 兜底原因，但不重复 prompt 原文
（原文在 states 的 `advice` 记录和 Web 看板里）。

`session_start` 追加了 `session_id` / `events_out` / `web_url` / `log_file`
四个键；旧消费者忽略即可。控制台文本另存为 `logs/live.log`（去 ANSI、加
墙钟时间）。详细 schema 与排查流程见
[docs/实时Web看板与日志排查-2026-11-05.md](docs/实时Web看板与日志排查-2026-11-05.md)。

### 5.2 `api.py`（`:8000`）

| 接口 | 说明 |
|---|---|
| `POST /ocr/table` | 单图完整识别（约 1.6s/张，推理串行加锁，单 worker ≈ 0.6 QPS） |
| `POST /ocr/cards` | 只读牌，不加载 OCR 模型（纯 OpenCV，快） |
| `POST /coach/advice` | 规则引擎建议，不走模型 |
| `GET /health` | 就绪探针：**OCR 模型加载完**才 `ready: true` |

响应语义（下游强依赖，改之前先看 README §响应语义）：
`table_size = 入局人数`；`seats` 从 hero 起沿行动方向绕一圈；`stack` 单位恒为 BB；
`status` 三态 `occupied/empty/uncertain`；没检出庄位仍是 **200**。

### 5.3 `replay-console`（`:8010`）

- 后端 FastAPI + PyMySQL，**不加载 OCR 模型**；前端 React 18 + Vite + AntD 5。
- 复用父项目 `poker_ocr.advice.build_situation()/advise()` 做重跑；决策点重建在 `replay/decisions.py`。
- `review_hands` **只读**，收藏/标签/结论只写 `replay_marks`；重跑产生 `replay_*` 表。
- 配置优先级：进程环境 > `replay-console/.env` > `poker-ocr/.env` > `call-back/.env`。
- 每条结果带版本快照（模型/采样参数/prompt_version/advice_version/桌风 sha/engine commit）。

---

## 6. 不可违反的口径（改代码前读这一节）

1. **坐标一律归一化，不 resize 整图。** 配置存 0–1，运行时乘实际宽高。`reference_size` 只作溯源。
2. **筹码必须过 BB 单位闸。** `238BB` 读成 `2388` 这类静默错值比读不出来危险；单位不对一律判不确定。
3. **`status` 三态不合并。** 「座位空」与「有人但筹码读不出」是两件事，都输出 0 会让下游分不清。
4. **`position` ≠ `is_dealer`。** 单挑时按钮位下小盲，庄家的 `position` 是 `SB`。
5. **`seats_ring_from_hero` 是座位环序，不是行动顺序。**
6. **庄位找不到就报找不到。** 检出两个报 `ambiguous`，绝不取最大响应蒙混。
7. **`errors` 非空 ⇒ 丢帧。** 遮挡/动画/误读的帧不进入手牌记录（用户明确要求）。唯一例外是**单座下注读到不可能的值**（`bet > pot`，底池就是全场投入之和）：这一位降级成「本帧下注未知」（`bet_unknown`），帧可以用，但这一帧不给建议、不自动点击（见 `docs/live-stream.md` §6.9）。
8. **金额一律用「本街加到总额」**（hand_track / call-back / coach 同口径）。
9. **fail open，不阻塞主链路。** 牌力/范围/画像任何一步缺数据都退化为「没这条事实」，不抛异常。
10. **模型只能引用事实，不能自己断言。** 牌力由 `ground_truth` 给；尺度由 `options` 给；模型只选 id。
11. **动作徽章不进变化指纹**（`frame_state.signature`），否则每个动作都算新状态。
12. **提示词改动必须 bump `PROMPT_VERSION`**（见 §7）。
13. **策略数字只改 JSON，不改 `.py`。** 默认 `hand-tuned`（v0）；`sourced`（v1）是实验开关，不是升级。
14. **玩家状态只有三态：在局（默认）/ 等待入局 / 弃牌。** 名单锁定的座位默认在局；「等待入局」只由头像文案（等待审核 / 等待中 / 正在带入）触发；「弃牌」只由「头像出现弃牌 + 手牌图标连续 2 帧消失」两条同时满足触发。中途任何槽位读不到只丢帧、不改状态；离桌只认头像「+」占位，位置按名单锁保留，不随人数重排。

---

## 7. 提示词系统

- 当前版本：`PROMPT_VERSION = "lean-zh-2026-11-05.1"`（[poker_ocr/prompt/version.py](poker_ocr/prompt/version.py)）。
- 只有**一套中文 lean**：不注入 Math gate、不做弃牌重问、不加载技能库。
- 版本号写进 `session_start` / advice JSONL；`replay/versioning.py` 的 `CURRENT_ADVICE_VERSION` 由它派生，**不要手工同步**。
- 改动 system、事实块或模块顺序 ⇒ 必须 bump，并在 `version.py` 顶部写清「为什么改、依据哪次复盘」。
- `advise()` 会把 `system/user/raw/reason_full` 与 `prompt_sha256` 一起带进 `Advice`；没调模型时显式标 `prompt_missing`。
- 事实优先级：**画像/本桌统计 > 现场观察**；未行动座位不得推测。
- 有 **golden test 锁死提示词哈希**：[replay-console/tests/test_prompt_golden.py](replay-console/tests/test_prompt_golden.py)。
  改了 system/user/事实块/选项规则却没同步版本号会直接失败。正确做法：
  1. bump `poker_ocr/prompt/version.py` 的 `PROMPT_VERSION`；
  2. bump `replay-console/replay/versioning.py` 的 `CURRENT_ADVICE_VERSION`；
  3. 更新 golden test 里的 `EXPECTED_ADVICE_VERSION` / `EXPECTED_PROMPT_HASH`。

---

## 8. 常用命令

```bash
# 环境（Python 3.10+，venv 在 .venv/）
.venv/bin/pip install -r requirements.txt

# 起单图识别服务
.venv/bin/python -m uvicorn api:app --host 0.0.0.0 --port 8000
#   生产：.venv/bin/gunicorn -c gunicorn.conf.py api:app

# 实时推流（默认 cam:auto，自动点击需确认；默认开 Web 看板 :8020）
.venv/bin/python -m poker_ocr.live \
    --profile config/table_wpk_live.json --initial-blinds 1/2 \
    --out logs/live.states.jsonl
#   只看建议不点击：--no-auto-click    完全自动：--no-confirm
#   不看板/不写事件日志：--no-web --no-events --no-log-file
#   看板端口冲突：--web-port 8021

# 测试模式（按实战视频演练，不是实战）：日志写 logs/live.test.jsonl，手牌不进手牌库
.venv/bin/python -m poker_ocr.live --test \
    --profile config/table_wpk_live.json --initial-blinds 1/2

# 手牌识别倒查（读 states + events）
.venv/bin/python tools/hand_recognition_audit.py --top 20 \
    --html logs/audit.html --json logs/audit.json
.venv/bin/python tools/hand_recognition_audit.py --explain <hand_id>
#   走 MediaMTX：--url rtsp://127.0.0.1:8554/live   竖屏手机源：--rotate 90

# 跑全部测试（分层，从不需要图到需要图）
.venv/bin/python tests/run_all.py

# 单图 CLI
.venv/bin/python extract_table.py run --img images/... --out /tmp/rois.json

# 复盘控制台（后端 :8010，托管 replay_web/dist 构建产物）
cd replay-console && ./start.sh
#   前端开发态：cd replay-console/replay_web && pnpm dev

# 手牌导出 / outbox 同步
.venv/bin/python tools/export_live_hands.py
.venv/bin/python tools/sync_hand_outbox.py

# 策略表（默认 hand-tuned；切换必须用真环境变量，写 .env 不生效）
.venv/bin/python -m poker_ocr.strategy_tables --profile sourced   # 生效 profile + 每张表来源/是否回落
.venv/bin/python tools/export_strategy_tables.py --check          # JSON v0 ↔ 模块常数逐值相等
.venv/bin/python tools/strategy_table_diff.py --tables            # 表级 v0/v1 对比
STRATEGY_TABLES_PROFILE=sourced .venv/bin/python -m poker_ocr.live ...

```

---

## 9. 已知的坑

- **`rois.json` 被 `scan.py run` 覆盖**：想保留结果就 `--out 别处.json`。
- **`scan.clean_number()` 有静默错值路径**：单位丢失时返回错值且 `ok=True`。新管线绕开了，别在新代码里用它。
- **`name_score` 不是「读对概率」**：形近字错读一样能到 0.9+。低于阈值只打 `name_review` 供人工看，不自动纠正。
- **3/8 的闭合孔数不是硬保证**：低分辨率/JPEG 会把 3 的开口糊成 1–2 个假孔，也会把 8 的孔糊掉。现在用 `glyph_left_indent`（中段最左墨迹）判左开口兜底；6 的凹口在右侧，不能用凸包缺陷当左开口（会把 6 误判成 3）。
- **同名不同人不做身份识别**：名字只是字符串。
- **`board_range.build`（旧）与 `range_flow`（新）语义不同**：旧路径按街一次过滤会丢掉上一街的收窄信息；新代码用 `range_flow`。
- **`table_default` 的 `region_gate.y_max=0.855` 会切掉 WPK 竖屏 hero 的筹码胶囊**：所以 `table_wpk_live.json` 改成 `0.99`。
- **onnxruntime 的 session 不保证线程安全**：`api.py` 用一把锁串行推理（6 并发结果一致但排队）；要吞吐只能多进程（每个 worker 各加载一份模型，内存翻倍）。
- **OBS 虚拟摄像头固定 1920×1080**：手机源竖屏铺满时必须 `--rotate 90`，否则 ROI 被横向压扁。
- **`STRATEGY_TABLES_PROFILE` 写进 `.env` 会静默不生效**：表在 `import` 时读，而 `.env` 是 `live.py` 跑起来之后才加载。必须用真环境变量。
- **v0 → v1 不是升级，是实验开关**：v1 只在 3 张表（`facing_probs` / `base_fold` / `size_gamma`）上有值，其余 23 张回落 v0；默认必须留 `hand-tuned`。

---

## 10. 改代码的检查清单

1. 改 `poker_ocr/` 核心逻辑 → 跑 `tests/run_all.py`，确认受影响层的测试先绿再跑全量。
2. 改识别口径/几何 → 同时看 `config/table_*.json` 与 `docs/` 对应设计文档，并核对 `README.md` 的契约段。
3. 改提示词 → bump `PROMPT_VERSION` + `CURRENT_ADVICE_VERSION` + golden test 的哈希（见 §7），再用 `replay-console` 对同一批手牌做 A/B（`tools/replay_dev_hands.py` / `tools/ab_range_set.py`）。
4. 改手牌记录结构 → 同步 `hand_track` → `hand_outbox` → `replay-console/replay/importer.py` 三段，否则坏手会被隔离在 `failed/`。
5. 新增配置项 → 写进 `config/` 并在 `profile.py` 校验（缺字段要 fail fast，不要静默默认）。
6. 改 `api.py` 响应字段 → 下游（控制台、导出工具）会静默错，先 grep 调用点。
7. 改策略数字（弃牌率 / 似然 / 弹性）→ 改 `poker_ocr/tables/*.json`，不要改 `.py`；跑 `tools/export_strategy_tables.py --check` + `tests/test_strategy_tables.py`。

---

## 11. 文档索引（按主题）

| 主题 | 文档 |
|---|---|
| 全景架构图 | [docs/archify/poker-ocr-overview.html](docs/archify/poker-ocr-overview.html) |
| 复跑控制台 | [docs/复跑控制台-设计文档.md](docs/复跑控制台-设计文档.md)、[replay-console/README.md](replay-console/README.md) |
| 手牌库与自动沉淀 | [docs/手牌库-自动沉淀与AB复盘-设计.md](docs/手牌库-自动沉淀与AB复盘-设计.md)、[docs/手牌库-实现说明-2026-11-02.md](docs/手牌库-实现说明-2026-11-02.md) |
| 对手画像/范围 | [docs/对手类型识别-设计.md](docs/对手类型识别-设计.md)、[docs/对手范围-事件模型-设计.md](docs/对手范围-事件模型-设计.md) |
| 实时牌力 | [docs/实时牌力解析-接入AI建议-设计.md](docs/实时牌力解析-接入AI建议-设计.md) |
| 事实注入 | [docs/ground-truth-事实注入-实现说明.md](docs/ground-truth-事实注入-实现说明.md) |
| 提示词 | [docs/提示词模块拆分-2026-11-03.md](docs/提示词模块拆分-2026-11-03.md)、[docs/提示词全量梳理-2026-11-03-冲突与缺口.md](docs/提示词全量梳理-2026-11-03-冲突与缺口.md) |
| 策略表（P0/P1） | [docs/策略表-P0-P1-2026-11-05.md](docs/策略表-P0-P1-2026-11-05.md)（抽表、推导模型、v0/v1 实测差异） |
| 弃牌曲线 / 裁判 | [docs/弃牌曲线探针-2026-11-05.md](docs/弃牌曲线探针-2026-11-05.md) |
| 复盘记录 | `docs/复盘-*.md`、`docs/复跑-*.md`（每次线上问题的事实来源） |
| 实时看板 / 日志排查 | [docs/实时Web看板与日志排查-2026-11-05.md](docs/实时Web看板与日志排查-2026-11-05.md) |

---

## 12. 怎么问 AI 最快（本仓库推荐姿势）

仓库已建 CodeGraph 索引（`.codegraph/`，随文件变更自动同步，滞后约 1s）。
**流程式提问**比「这个文件干啥的」有效得多：

| 你问 | AI 该用 |
|---|---|
| 「一手牌从帧到建议经过哪些层」 | `codegraph_explore`（一次调用返回跨文件源码 + 调用路径） |
| 「改 `Situation` 会影响谁」 | `codegraph_impact` |
| 「谁调用了 `advise`」 | `codegraph_callers` |
| 「`parse_chip` 的签名和实现」 | `codegraph_node` |

局限：跨文件解析按名匹配，重名可能多候选；图谱不做正确性验证 —— 最终以 `tests/run_all.py` 为准。
