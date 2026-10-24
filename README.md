# 牌桌截图状态提取

9 人德州扑克桌截图 → 一圈座位状态（**名称 / 位置 / 筹码**），从自己开始沿行动方向
绕一圈到自己的上家。

## 起服务

```bash
.venv/bin/python -m uvicorn api:app --host 0.0.0.0 --port 8000
```

接口文档自动生成，可直接给对接方看：<http://localhost:8000/docs>

```bash
curl -X POST http://localhost:8000/ocr/table -F "file=@table.jpg"
```

```json
{
  "table_size": 9,
  "stack_unit": "BB",
  "seats": [
    {"name": "svsvbmbm",   "position": "BTN",   "stack": 460.5, "is_me": true},
    {"name": "无敌猪肉佬", "position": "SB",    "stack": 302,   "is_me": false},
    {"name": "陈凤",       "position": "BB",    "stack": 891,   "is_me": false},
    {"name": "奥利奥H",    "position": "UTG",   "stack": 141.5, "is_me": false},
    {"name": "稚气未酒",   "position": "UTG+1", "stack": 113,   "is_me": false},
    {"name": "夏至美食",   "position": "UTG+2", "stack": 63,    "is_me": false},
    {"name": "柚柚柚子呦", "position": "LJ",    "stack": 331,   "is_me": false},
    {"name": "悍将v1",     "position": "HJ",    "stack": 245,   "is_me": false},
    {"name": "到晚上就想", "position": "CO",    "stack": 163,   "is_me": false}
  ],
  "warnings": []
}
```

## 实时推流识别

默认直接读 **OBS 虚拟摄像头**（`--url cam:auto`，同一台 Mac 延迟最低）；也可以
用 `--url rtsp://127.0.0.1:8554/live` 走 OBS ──SRT──▶ MediaMTX ──RTSP。
worker **不节流**：处理完当前帧立刻取邮箱里的最新帧，中间帧直接丢弃；用固定
ROI 识别座位、筹码、下注、动作、庄位、底池和盲注，输出一行一个状态的 JSONL。

**完整方案、功能、OBS 参数、标定流程、输出字段与排障见
[docs/live-stream.md](docs/live-stream.md)。**

### 快速启动（虚拟摄像头 + 自动点击）

默认 `--url cam:auto`、`--auto-click` 开启。自动点击需要先标定一次按钮位置：

```bash
# 0) 只做一次：标定桌面客户端的虚拟按钮（默认圆形，按 C 切换方形）
.venv/bin/python tools/calibrate_clicker.py --list-displays
.venv/bin/python tools/calibrate_clicker.py --out config/clicker_local.json

# 1) 建议先 dry-run：只打印将点击的随机坐标，不发送真实鼠标事件
.venv/bin/python -m poker_ocr.live \
  --profile config/table_wpk_live.json \
  --initial-blinds 1/2 \
  --out logs/live.states.jsonl \
  --clicker-dry-run

# 2) 真实运行（默认 cam:auto + 自动点击；每次点击前单键确认 y/n）
.venv/bin/python -m poker_ocr.live \
  --profile config/table_wpk_live.json \
  --initial-blinds 1/2 \
  --out logs/live.states.jsonl

# 完全自动、不再询问：追加 --no-confirm
```

- `--url` 默认 `cam:auto`（自动选择 OBS 虚拟摄像头，索引用 `tools/list_cameras.py` 查）；
  走 MediaMTX 时加 `--url rtsp://127.0.0.1:8554/live`，并先
  `mediamtx config/mediamtx.yml`。
- `--auto-click` 默认开启；只要 AI 建议、不要自动点击时加 `--no-auto-click`。
- 自动点击默认是**交互确认**模式：模型给出建议后终端问 `y` 确认 / `n` 拒绝。
  想回到「结果返回立刻点」的无人值守模式，加 `--no-confirm`。
- 关闭 AI 建议时（`--no-ai-advice`）自动点击会跟着关闭。
- 没有 `config/clicker_local.json` 时启动会明确报错，先跑第 0 步。
- 真实点击需要 macOS「辅助功能」权限，见下方自动点击小节。

**虚拟摄像头读不到 / 解析不出状态时**：

```bash
# 1) 确认选中的是 OBS，而不是内置/FaceTime 摄像头
.venv/bin/python tools/list_cameras.py
# 看 /tmp/poker_cameras/camN.jpg，必须是牌桌画面；不是就改 OBS 场景/来源

# 2) OBS 虚拟摄像头固定输出 1920x1080。若手机源在 OBS 里旋转 90° 铺满，
#    worker 必须加 --rotate 90；否则 ROI 会被横向压扁，OCR 读不出筹码/名字
.venv/bin/python -m poker_ocr.live --rotate 90 ...
```

启动日志里的 `[比例]` 会打印实际 ROI 尺寸；出现
`ROI 分辨率只有参考图的 49%` 或宽高比偏差警告时，OCR 基本读不出状态，
应改 OBS 画布比例、加 `--rotate`，或改用 RTSP 路线
（`--url rtsp://127.0.0.1:8554/live`）。

### AI 实时建议（默认开启）

worker 在**轮到我**时把这一手的完整行动线交给模型（`--no-ai-advice` 可关闭）。
默认用 **lean 提示词**：输出契约 + 程序算好的事实（位置/筹码/赔率/牌力/听牌）
+ 桌面动态 + 一份紧凑的现代范围框架（按位置 RFI、3bet/冷跟、翻后纹理与尺度）。
翻后牌力不再只给「顶对/顺子」这种名字，而是注入一段 **GROUND TRUTH**：最好的五张、
用了哪两张底牌、精确枚举的 `beaten_by`、坚果与否、听牌 outs、牌面「成花可能 / 只是花听」。
模型只允许引用，不允许自己重算（见 `poker_ocr/ground_truth.py`）。
翻后还会注入一组几何事实：底池类型（单加注 / 3-bet / 溜入）、**SPR**、我在不在位置、
身后还有几人要行动；短码 all-in 会单独标注「side pot 未建模、跟他只有这么多筹码有风险」。
反过来，对手全下 cover 我时，超出我后手的筹码会退回，赔率分母用**可争夺底池**
（`winnable_pot()`，已扣掉这部分）而不是牌桌显示的总池。
听牌面对下注时，程序先算好 **Math gate**（权益 vs 赔率门槛；隐含赔率按 0，不夸大
潜在收益）并给出 call / fold 结论；模型若在 +EV 闸门下仍弃牌，会带着结论重问一次，
最终仍弃牌则记 `gate_violation` 供复盘。模型输出除 `option` / `reason` 外还有
`plan`（下一街意图）与 `confidence`；`plan` 会在同一手的下一街回灌成
`My previous decision`，避免翻牌说要抓诈、转牌又自己弃掉。
旧的 2005 年《Little Green Book》技能库不再默认注入，需要对比时加
`--advice-style skills` 切回。模型从固定选项里选一个：

```
翻后：弃牌 / 跟注（没人下注时是过牌）/ 加注到底池 1/3 / 1/2 / 2/3 / 1 倍 / 1.2 倍
翻前：弃牌 / 平跟（或过牌）/ 开池到 2.5-4BB（按位置档；有前注 ×2、前两个溜入者各 +1BB、第 3 个起每个 +0.5BB、松桌再 +0.5BB）/ 面对加注时 3× 或 4× 上家加注
```

翻前不用底池倍数：底池只有盲注那么大，而且 `preflop-open.md` 明确要求尺度用
bb 说。金额由程序按现场底池/加注算好（含最小加注额与后手封顶），模型只返回
选项 id，所以它不可能给出做不出来的尺度。模型调用在后台线程跑，**不阻塞采样**；
结果写进 JSONL 的 `advice` 记录并高亮打印（含本次耗时）。等模型期间识别会暂停，
终端每秒打一行 `[建议] 生成中… Ns` 心跳，日志不会静默几秒后突然成团出现。

```bash
cp .env.example .env      # 然后编辑 .env，填入 AI_SERVER_API_KEY

# 默认 cam:auto；这里 --no-auto-click 表示只看建议、不自动点
.venv/bin/python -m poker_ocr.live \
  --profile config/table_wpk_1080x2400.json \
  --initial-blinds 1/2 --ai-advice --no-auto-click \
  --out logs/live.states.jsonl
```

`.env` 默认从**项目根目录**读，找不到再退到启动时的 CWD；`ENV_FILE=/path/to/.env`
可以显式指定（指定了却读不到会直接启动失败）。**环境变量优先**：已经 export
的同名变量不会被 `.env` 覆盖。`.env` 已被 `.gitignore` 忽略，模板是 `.env.example`。

**牌桌动态 `.table_dy`（可选，只写桌级）**：在项目根目录放一个纯文本 `.table_dy`，
描述当前桌子**整体**是什么样，例如：

```text
整体偏松，入池的人多，翻后经常多路；少诈唬，价值下注为主。
```

以 `#` 开头的行是注释，空行忽略；内容会作为 `Table context` 注入每次 AI 建议的
提示词。**这个文件不写玩家名字，也不写单个玩家的风格**：每个对手的类型由画像系统
按座位统计，人工覆盖用 `--pin`。桌级信息只描述整桌倾向（松紧、多路程度、抽注结构、
默认剥削方向），逐座位画像永远优先于它。文件每次决策点重新读取（按 mtime 缓存），
**改完下一手就生效**，不用重启；文件不存在时按「未知对手」标准线，不影响启动。
`TABLE_DY_FILE` 可以显式指定路径；启动日志和 JSONL 的 `session_start` 会记录本次
生效的动态。

**对手类型画像（默认开启）**：每手结束后，用这一手的行动线 + 摊牌记录更新
每个对手的本地统计（VPIP / PFR / 3bet / AF / 翻后激进 / open 与下注中位数 /
摊牌牌型），再用规则评分分成 `station`（松弱/跟注站）、`lag`（松凶）、
`maniac`（松浪）、`nit`（紧弱）、`tag`（紧凶）。决策时按**本手在局对手**
取画像：面对下注看最后一个下注的人，无人下注时看还在后面、可能加注的
LAG/maniac；样本不足的 `unknown` 不改变建议。画像存在
`data/opponents/profiles.json`（本地、gitignore、原子写），不调用模型。

```bash
# 默认开启；--no-opponent-profile 回退到 .table_dy
.venv/bin/python -m poker_ocr.live --ai-advice \
  --opponent-db data/opponents/profiles.json

# 启动时清空当前池画像
.venv/bin/python -m poker_ocr.live --ai-advice --reset-opponents
```

优先级：**人工 pin > 高置信自动画像（n≥15 且 conf≥0.65）> 桌级 `.table_dy` >
unknown 基线**。`n<8` 一律 unknown；8–14 只标 `tentative`，
只软提示、不改硬指令。手牌记录新增 `street_active` / `players_dealt` /
`seat_meta`，因此即使弃牌徽章漏读，也能从「街初在局、街末不在局」补出
弃牌，算出真正的 VPIP 分母。爆炸底池的 5BB 强制前注属于**被动入池**：
不补翻前 check、不计入 VPIP 分母，单独记在 `bomb_pot_hands` /
`passive_entry_hands`，避免「人人都在池里」把 VPIP 拉低。旧日志可重放补全：

```bash
# 手牌记录 → 画像（JSONL 里有 state 帧会自动重放进 LiveHandTracker）
.venv/bin/python tools/rebuild_opponent_profiles.py \
  --hands hands/1023.hands.json logs/live.states.jsonl \
  --out data/opponents/profiles.json

# 人工修正 / 查看
.venv/bin/python tools/rebuild_opponent_profiles.py --pin "芒果与西米=maniac"
.venv/bin/python tools/rebuild_opponent_profiles.py --unpin "芒果与西米" --list
```

**爆炸底池识别**有两条线索，任一成立就写入 `Explosive pot: YES`：

1. **屏幕横幅（权威）**：爆炸底池开局屏幕中央会弹「幸运暴击」四个黄字 +
   「BOMB POT」英文。程序把这两行拆成两条紧 ROI 跑纯识别（`bomb_cn` /
   `bomb_en`），英文读到 `BOMB POT` 或中文四字命中三个即确认；横幅只在开局
   出现约 2 秒，所以看到后就钉在**整手牌**上，换手才清空。人数或底池读数
   有偏差时（空座、OCR 误读）横幅仍然权威。
2. **底池推算（兜底）**：翻后「上一街结束时的底池 ÷ 实际参与人数」人均
   **刚好 5BB**。炸弹池前注固定 5BB/人，所以总额能被人数整除（8 人 40BB、
   9 人 45BB）；普通大池人均可以更高但不会整除。用于横幅没抓到的手
   （中途接管、动画被遮挡/丢弃）。

触发后提示词会写 `Explosive pot: YES` 并附上依据（横幅 / 算式），同时启用
专门的系统提示：这种池子所有人都进了池，翻后会有大量歪中的两对/三条；**顶对/
超对只是中等牌** —— 不主动加注、不 stack off，面对加注/大注/多街连续下注
直接弃，没人下注时小注控池；两对/三条/顺子/同花等强成牌仍正常拿价值。

**模型协议由模型名决定**：

| `AI_SERVER_MODEL` | 协议 | `AI_SERVER_BASE_URL` |
|---|---|---|
| `typesafe/jev-1.14`、`jev-*`（名字里带 jev） | JEV decisions：`state` + 带类型的 `questions` → `answers` | **完整**地址，如 `https://openrouter.ai/api/alpha/decisions` |
| 其他（`deepseek-flash` 等） | OpenAI 兼容 `/chat/completions` | 基址，如 `https://api.deepseek.com` |
| OpenRouter 上的 GPT（如 `openai/gpt-5.6-luna`） | 同上，额外支持 `reasoning` | 基址 `https://openrouter.ai/api/v1` |

JEV 模式下候选动作会被转成一道 `choice` 题（criteria 的 key 就是选项 id），
返回值仍是 `{option, reason}`，所以下游与 DeepSeek 完全一致；`confidence`
（选择概率）只写日志，不进 JSONL。`AI_SERVER_REFERER` / `AI_SERVER_TITLE`
是 OpenRouter 的可选排名头（JEV 和 chat 模式都会带）。

**OpenRouter GPT 的推理**：`.env` 里设 `AI_SERVER_REASONING=enabled`，请求体发
`"reasoning": {"enabled": true}`（配了 `AI_SERVER_REASONING_EFFORT` 时会嵌成
`reasoning.effort`）。响应里的 `reasoning_details` 会被原样保留在
`complete_json` 的返回值里；`advice.chat_message` 返回完整的 assistant
message，多轮续推时把它连同 `reasoning_details` 放回 `messages` 即可继续
推理，不要裁剪或改写这个字段。开推理会明显拉长单次耗时，实时链路记得把
`AI_SERVER_TIMEOUT` 调到 15–30 秒；推理模型通常只接受默认 temperature，
这一段不要设 `AI_SERVER_TEMPERATURE` / 太小的 `AI_SERVER_MAX_TOKENS`。
`.env` / `.env.example` 里都有可直接取消注释的 GPT 配置段（与 JEV 共用同一个
OpenRouter Key）。

**403 `provider Terms Of Service` 不是 Key 错**：同一把 Key 调 `/api/v1/key`
和 Llama/DeepSeek 都正常、只有 OpenAI 模型被拒时，是 OpenRouter 账号/地区/
供应商条款对 OpenAI 模型的限制（OpenAI 供应商对部分地区直接拒绝）。去
OpenRouter 的 Activity 页看被拒原因，或换一个有 OpenAI 模型权限的账号/Key；
代码侧不用改。`_http_error` 已把 403 和 401 分开报，日志里能直接看到这个提示。

- **「轮到我」默认看 hero 头像槽的蓝色「下注/加注」按钮**（`images/roi/hero_bet.jpg`
  实测蓝色占比 0.85–0.86，非回合帧 0.000–0.003），所以 `table_wpk_live.json`
  没有 `hero_turn_probe` 也能用；老 UI 会回退到预设钮文本判据。
- 只支持 `--recognizer roi`：全图路线读不到每座的本街下注，算不出跟注额。
- 换手时另外写一条 `hand` 记录，含这一手完整的逐街行动线（对手 + 自己）、
  这一手给过的全部 AI 建议（`advices`），以及摊牌/主动秀牌时读到的对手手牌
  （`shows`，例如 `{"seat_id":"midL","name":"许文文啊","cards":["Ac","Qh"]}`；
  河牌只主动秀一张时 `cards` 只有一张）和全下汇总（`all_ins`，含全下金额与
  界面实时胜率；全下需连续 2 帧确认，避免亮牌牌背的单帧误判），可以直接拿去
  复盘或对照。完整手牌落库时终端会打一条**亮黄底**提示（如
  `完整手牌 7，id h7 日志已经生成`），AI 决策有问题时凭这行就能按 `hand_id`
  回到 JSONL 定位。
- 上一手收牌到下一手发牌之间，追踪器会开一手**没有 hero 手牌、也没有行动线**
  的「空档手牌」；这种记录默认不写，否则每手牌后面都会多出一条空记录。
  需要**旁观/观察对手**时加 `--record-without-hero`（或在 `.env` 里写
  `RECORD_WITHOUT_HERO=1`），没读到自己手牌的手牌也会完整落库。
  跳过条数可在 `session_end.summary.hands_skipped_no_hero` 看到。
- 头像上的黄色「All-in」徽章会被识别成 `all_in`；全下摊牌时筹码/下注位的
  百分比会写入 `equity`。徽章从 All-in 变成其他状态时，按这手结束处理，
  不必等庄位/牌面变化。
- 公共牌/hero 手牌过跨帧稳定闸：公共牌只前进（**4→3、5→4 永不回退**），
  hero 只接受 0/2 张，新读数连续 2 帧一致才替换 last-good；牌背/遮挡/
  低置信度不参与替换。带疑点的读数会开 `--card-doubt-window`（默认 0.5s）
  的**疑惑帧**窗口做多帧仲裁：窗口结束时挑战者票数占优（≥2 票且 ≥60%）才
  替换 last-good，否则保留旧牌；牌背/遮挡/`?` 永远不能靠票数转正。
  当前帧有疑点时 `reliable=False`，稳定结果不可靠就**跳过这一帧的 AI 建议**，
  手牌记录里带 `cards_unreliable` / `cards_warnings`，牌面恢复后同一决策点补投。
  公共牌回退/张数异常/牌背**持续 1 秒以上**时，最近帧才落到
  `logs/incidents/`（同类持续异常只存一份，单次会话默认最多 20 份；
  `--no-incident` 关闭）。
- 模型超时/报错/返回了不在选项里的答案时，记录标 `source=fallback`：
  面对下注→弃牌，否则→过牌（最保守），并保留 `error` 原因，绝不猜。

### 桌面自动点击（默认开启）

AI 建议返回后，可以在客户端的对应按钮上自动点一下。先把按钮位置标定成
**虚拟区域**（屏幕逻辑点，默认圆形；按 `C` 可切换成方形），点击时在区域内缩
一圈的范围里取随机坐标；圆形按钮的随机点取在内切椭圆里，不会落到四角空白：

| 建议 | 点击 |
|---|---|
| 弃牌 | `fold`（左侧弃牌） |
| 跟注 / 过牌 / 跟全下 | `call`（右侧跟注/让牌） |
| 加注 / 下注 | 点五个快捷档位之一（底池 1/3、1/2、2/3、1、1.2）；点档位即完成下注/加注，不再点蓝色确认按钮 |

翻前建议（`open-2.5`、`raise-3x` 等）没有对应的底池档位，按**金额最近**的档位点。

默认是**人工确认**模式：建议打印出来后在终端问
`[确认] flop 建议 Call 4BB，是否执行？`，**按 `y` 才点，按 `n` 拒绝**。
单键读取、不用回车；其他按键（含回车）一律忽略，继续等 `y`/`n`。每次提示
出现前会清空终端里缓存的按键，模型生成期间提前敲的键不会误触这次确认。
确认期间主循环会等待（等同于模型生成期间的暂停），所以建议在看到提示后
尽快作答。想让模型结果返回后立即执行，加 `--no-confirm`。

```bash
# 1) 标定：客户端窗口摆好后，按提示依次拖出按钮外接框
#    默认画圆形，按 C 切换圆形/方形；--shape rect 可让新按钮默认方形
.venv/bin/python tools/calibrate_clicker.py --list-displays
.venv/bin/python tools/calibrate_clicker.py --out config/clicker_local.json

# 2) 先 dry-run：只打印随机坐标，不真的点；建议返回时仍需 y/n 确认
.venv/bin/python -m poker_ocr.live \
  --profile config/table_wpk_1080x2400.json \
  --initial-blinds 1/2 --clicker-dry-run

# 3) 确认坐标无误后真实运行（--auto-click 默认开启，建议前会问 y/n）
.venv/bin/python -m poker_ocr.live \
  --profile config/table_wpk_1080x2400.json \
  --initial-blinds 1/2

# 不需要自动点击时：
#   --no-auto-click
# 不需要每次确认、结果返回立刻点击时：
#   --no-confirm
# 走 MediaMTX 而不是虚拟摄像头时：
#   --url rtsp://127.0.0.1:8554/live
```

**macOS 权限**：真实点击需要「辅助功能」权限，授予**运行 Python 的终端 /
iTerm / IDE**（不是 Python 本身），然后完全退出并重开它。也可以先跑
`tools/calibrate_clicker.py --request-access` 让系统弹授权框。没有权限时启动会
直接报错，不会静默不点。

**安全闸**：只有当前帧仍然 `hero_turn=true`、`state.ok`、决策点与投递时一致、
手牌一致、牌面可靠、且这条建议没点过，**并且终端确认了 y**（`--no-confirm`
时跳过最后一条），才会执行；模型超时/回退建议默认不点
（`clicker_local.json` 里 `allow_fallback: true` 可打开）。退出/换手/断流重连
的 drain 只落库不点击。每次点击结果写在 `advice` 记录的 `click` 字段里；
用户按 `n` 时记为 `status=skipped, reason=user_rejected`。

**生效确认与重试**：点击后约 0.5s，用下一帧的动作指纹（`hero_turn`、每座动作
徽章、底池/下注/筹码等）和点击时对比；页面已经变化就不再动，**没变化会再执行
一次**。重试次数和延迟可在 `config/clicker_local.json` 里改
（`retry_delay_ms`，默认 500；`retry_count`，默认 1；设 0 关闭）。重试结果写进
`click_retry` 记录，并计入退出汇总的 `click_retries`。

**注意**：坐标是绝对的，客户端窗口移动/缩放、改分辨率或显示器排列后要重新标定；
自动点击不会把客户端拉到前台，运行时要让客户端窗口保持最前。客户端建议用
**窗口模式**（不要 macOS 全屏/独立 Space），否则标定遮罩可能和客户端不在同一个
Space。真实鼠标事件没有送达回执，请先在模拟桌/非真钱环境验证。配置默认写到
`config/clicker_local.json`，已被 `.gitignore` 忽略。

### 抓帧 → 裁剪 → 验证 → 重新标注

同一台设备/OBS 布局下，建议先用真实帧重新标定一次：

```bash
# 1) 按 0.5s 抓 200 张
.venv/bin/python tools/capture_stream.py \
  --every 0.5 --max-frames 200 --out logs/live_frames

# 2) 裁黑边 + 按 profile 参考比例对齐
.venv/bin/python tools/crop_frames.py logs/live_frames \
  --out logs/live_frames_cropped

# 3) 用现有 profile 跑 ROI，输出叠加图和报告
.venv/bin/python tools/validate_roi.py logs/live_frames_cropped \
  --profile config/table_wpk_1080x2400.json \
  --initial-blinds 1/2 \
  --out-dir logs/roi_validation

# 4) 重新标注 ROI，生成当前布局的 profile
.venv/bin/python tools/refit_profile.py logs/live_frames_cropped \
  --profile config/table_wpk_1080x2400.json \
  --out config/table_wpk_live.json --step 2
```

### 输出

每行一个 JSON：`session_start` / `stream_info` / `state` / `hand` / `advice` /
`session_end`。默认**追加**到 `--out`，重启不会清掉历史手牌；需要每次覆盖时加
`--no-append`。
`state` 里包含：

- `players[]`：每座的 `position/name/stack/bet/action/fold/in_hand/all_in/status`
- `dealer` / `hero_turn`
- `pot` / `pot_before_street` / `street_bet_total`
- `blinds`（`sb/bb/ante`，单位 BB，支持跨帧继承）

一帧里任何一个字段无法精准识别就整帧丢弃（不写输出）。示例和字段语义见
[docs/live-stream.md §6](docs/live-stream.md#6-输出-jsonl)。

### 其他路线

```bash
# 浏览器 / 布局不固定：全图标定一次 → 运行时 ROI
.venv/bin/python tools/calibrate_live.py --frames 5 --crop auto \
  --out config/runtime_browser.json

.venv/bin/python -m poker_ocr.live --recognizer roi \
  --profile config/runtime_browser.json --crop auto \
  --initial-blinds 1/2 --lenient-chips --out logs/live.states.jsonl

# 直接走全图（慢，但不依赖固定槽位）
.venv/bin/python -m poker_ocr.live --recognizer full \
  --full-config config/full_browser.json --out logs/live.states.jsonl

# 离线视频
.venv/bin/python tools/video_to_states.py images/video/video1.mp4 \
  --every 0.5 --out /tmp/video1.states.json
```

## 命令行

```bash
.venv/bin/python extract_table.py run table.jpg
```

结果会打印出来，**同时写进 `rois.json`**（每次 run 都写，可用 `--out` 改路径）。
CLI 和 API 走的是同一条管线，输出字段略有不同（CLI 的 `seats_ring_from_hero` 字段更全，
带置信度和复核标记；API 只透出对接需要的部分）。

## 安装

```bash
cd ~/codes/poker-ocr-poc
~/.local/bin/python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

---

## 实测结论（2026-09-30，1440×3200 竖屏截图 × 2 张）

**什么都不需要训练模型。** 全图 OCR + 几何分配 + 几何庄位检测，两张图各 27 个字段全对：

| 项目 | 结果 |
|---|---|
| 9 个筹码 × 2 图 | 全对，置信度 0.995–1.000 |
| 9 个名字 × 2 图 | 全对，含中英混排（`奥利奥H`、`悍将v1`）、纯英文（`svsvbmbm`）、**带数字**（`菜菜子31`、`Dragon333`、`谈轩66`）、**长名字**（`唐小鱼呀`、`八六上青山`、`保险能不能`）|
| 庄位 | 自动检出，认出「D」置信度 0.97，归给 hero |

### 两处「OCR 读错」其实是人看错了

`稚气未酒` 和 `菜菜子31` 都被当成过 OCR 错字，放大到 5–6 倍核对后确认 **OCR 是对的**：
前者末字是「酒」（右边是「酉」，能看到多出来的横），后者第三字是「子」不是「菜」
（前两个「菜」头顶有艹字头，第三个没有）。正常尺寸下这些汉字很容易看错，别急着怪 OCR。

`稚气未酒` 这个末字一开始被误判成 OCR 错字，放大到 6 倍核对后确认**是「酒」不是「洒」**，
OCR 没读错。

噪声全部被几何门挡在外面，没有一个混进座位：状态栏（`21:44`/`7.67 KB/S`）、
播放器 UI（`X1`/`10/20`/`ID:...`）、牌面标签（`翻牌`/`转牌`）走区域门，
`总底池` 走槽位门。

---

## 用法

### 1. 核对坐标（改配置后先跑这个）

```bash
.venv/bin/python extract_table.py slots table.jpg --out slots.png
```

只画槽位不跑 OCR，几秒钟。打开 `slots.png` 确认 9 组绿框（名字）、黄框（筹码）、
品红框（头像）严丝合缝。**框不对后面全错，这一步不过别往下走。**

### 2. 提取

```bash
.venv/bin/python extract_table.py run table.jpg --dealer-seat upL
```

常用参数：

| 参数 | 用途 |
|---|---|
| `--dealer-seat upL` | 人工指定庄位。不指定就走自动检测（找白底 D 圆片） |
| `--out PATH` | 结果落盘位置，默认 `rois.json` |
| `--hero-seat midR` | 覆盖 hero 座位，环序会跟着旋转 |
| `--dump-slots [PATH]` | 存槽位叠加图 |
| `--dump-seats DIR` | 存每个座位的名字裁剪 + 接触表，复核用 |
| `--no-ladder` | 名字只认原始结果，不跑放大阶梯（排查用） |
| `--strict` | `valid=false` 时非零退出 |
| `--no-debug` | 输出不带 debug 字段 |

### 3. 回归测试

```bash
.venv/bin/python tests/run_all.py
```

分三层，从不需要图到需要图：

| 套件 | 验的是 | 要图吗 |
|---|---|---|
| `test_positions.py` | 位置推导：2–9 人桌 × 庄位每个位置（72 组合）、环旋转无关性、单挑特例、与 call-back 的契约一致性 | 否 |
| `test_dealer.py` | 庄位检测：**该不检出时不检出**（空白桌面、筹码大小、文字大小、离座位太远）、两个候选报歧义、正确尺寸归最近的座位 | 否（合成图） |
| `test_extract.py` | 端到端：9 座位 × 6 字段 + 噪声归属 + 庄位唯一性 + 位置集合 | 是 |
| `test_api.py` | HTTP 接口：响应契约、错误码、空桌、没庄位的降级、人工覆盖 | 是（TestClient） |

`test_extract.py` 自动发现 `tests/golden/*.expected.json`，每份 golden 自带 `image`
字段 —— **换图或加图只要丢一份 golden 进去，不用改测试代码**；图不在就跳过并提示，
图放回来会自动重跑。

`test_dealer.py` 用合成图而不是照片，是因为「没有庄位时不能误报」这条最要紧：
误报一个庄位会让**整套位置跟着整体错位**，而且看起来还挺像那么回事。

---

## HTTP 接口

### `POST /ocr/table`

`multipart/form-data`，图片放 `file` 字段。

| 参数 | 位置 | 说明 |
|---|---|---|
| `file` | form | 牌桌截图（PNG/JPEG），上限 20MB |
| `dealer_seat` | form，可选 | 人工指定庄位座位 id。不指定就走自动检测 |
| `hero_seat` | form，可选 | 人工指定哪个座位是我。不指定用配置默认（底部中间） |
| `debug` | query，可选 | `?debug=1` 额外返回完整中间结果（各字段置信度、噪声框），排查用 |

响应里还包含 `board`（公共牌）与 `hero_cards`（我的手牌），每张带
`code` / `rank` / `suit` / `suit_name` / `confidence`，均按画面从左到右。
读牌逻辑见 `poker_ocr/cards.py`（颜色 + 形状判花色，字形模板判点数）。
`confidence` 取「花色置信度」和「点数模板分」里较低的那个；花色以颜色为硬判据，
形状只做校验，形状不一致时只轻降置信度并标记 `shape_agrees=false`，不会因为
轮廓阈值在边界抖动就把 K♣/Q♣/J♣ 这类牌判成不可用。

### `POST /ocr/cards`

只读牌，**不加载 OCR 模型**（纯 OpenCV），所以比 `/ocr/table` 快得多。

| 参数 | 位置 | 说明 |
|---|---|---|
| `file` | form | 牌桌截图（PNG/JPEG），上限 20MB |
| `debug` | query，可选 | `?debug=1` 返回逐张的像素框、花色/形状证据、点数模板分数 |

```json
{
  "board": ["7d", "9c", "10c", "6h", "8c"],
  "hero_cards": ["4c", "Ks"],
  "warnings": []
}
```

翻前 `board` 为空数组，**不是错误**。牌面是图形化的，OCR 读不出花色和 J/Q/K/A，
所以这里完全不走 OCR：花色按「♠黑 / ♥红 / ♣绿 / ♦蓝」的颜色 + 轮廓形状判，
点数按 `assets/card_rank_templates.png` 的字形模板做 NCC 匹配。

### `GET /health`

就绪探针。**OCR 模型加载完才返回 `ready: true`** —— 加载要一两秒，
容器编排里拿它当 readiness probe，别让流量在模型就绪前打进来。

### 响应语义

- **`table_size` = `seats` 的长度**，只数**入局的玩家**。9 人桌空了 2 个座位就是 8 人桌，
  位置表也跟着换成 8 人的。
- **「入局」的判据是筹码**：筹码读出来的座位才算在场。空座位、以及「看着像有人但筹码读不出」
  的座位都不发牌、不占位置、不算人数，**整个不返回** —— 没有筹码就是没有这个玩家。
  唯一的例外是 hero：那个座位是配置里写死的「我」，只要不是判成空座位就一定返回，
  哪怕筹码没读到（`stack` 为 `null`，`warnings` 里会点名让下游提示用户补）。
- **`seats` 从我（hero）开始**，沿行动方向绕一圈，**hero 恒在第一个**。
  每个座位都带 `position`，下游按位置找也行。
- **`stack` 是纯数字，单位是 BB。** 界面按 BB 显示，所以原样透出，顶层 `stack_unit` 标了单位。
  想换成筹码数得知道大盲是多少，而截图里没有这个信息。
  整数值保持整数（`302` 而不是 `302.0`）。
- **`is_me`** 恒有一个为 `true`。它和 `position` 是两件事：绝大多数时候「我」就是某个位置，
  但**单挑时按钮位下小盲**，「我」既是庄家又占着 `SB` 这个位置名。

### 失败怎么返回

| 情况 | HTTP | 表现 |
|---|---|---|
| 没传文件 | 422 | FastAPI 的参数校验 |
| 空文件 / 不是图片 | 400 | `detail` 说明原因 |
| 图片超过 20MB | 413 | |
| `dealer_seat` / `hero_seat` 不是合法座位 | 400 | `detail` 列出可选座位 id |
| **没检出庄位** | **200** | `position` 全为 `null`，`warnings` 说明原因 |
| 某个座位名字读不出 | 200 | 该字段 `null`，`warnings` 列出来 |
| 某个座位筹码读不出 | 200 | **该座位不返回**（按空座位处理，人数也不含它），`warnings` 说明 |

**没庄位不是请求错误**：名字和筹码仍然准确可用，不该因为一个字段推不出来就把整份结果丢掉。
真正读不出内容时 `warnings` 会逐条列明，下游据此决定要不要人工复核。

### 性能

单张约 **1.6 秒**（1440×3200，本机 CPU）。推理**串行执行**——onnxruntime 的 session
不保证线程安全，并发跑同一份模型可能互相踩内存，所以加了锁。
6 个并发请求实测结果完全一致、无异常，代价是排队（总耗时约 9.5 秒）。

要提吞吐就得多进程（`uvicorn --workers N`），每个 worker 各加载一份模型 ——
代价是内存翻倍，且需要确认 onnxruntime 在开多进程时的线程配置。
**当前单 worker 的容量大致是 0.6 QPS**，批量场景建议客户端排队或加队列。

---

## 命令行输出契约

```jsonc
{
  "frame_id": "table-wrap-d.jpg",
  "direction": "cw",              // 行动方向，已由盲注证实
  "direction_verified": true,
  "table_size": 9,                // 桌号 = 有筹码座位的数量，空座位不发牌
  "dealer": {
    "seat_id": "hero",
    "status": "found",            // found / not_found / ambiguous
    "method": "blob"
  },
  "valid": true,
  "violations": [],
  "seats_ring_from_hero": [
    {
      "ring_index": 0,
      "seat_id": "hero",          // 稳定 id，不随帧变，可作跨帧合并的键
      "position": "BTN",          // 由庄位推出，见「位置怎么算」
      "name": "svsvbmbm",
      "name_score": 0.9834,
      "name_review": false,
      "stack": 437.5,
      "unit": "BB",
      "stack_score": 0.9993,
      "status": "occupied",       // occupied / empty / uncertain（识别侧的判据）
      "in_hand": true,            // 算不算在场：有筹码才算。决定人数、位置、接口返不返回
      "is_dealer": false,
      "name_variants": [...]      // 4 级重试阶梯各自的结果
    }
    // ... 共 9 个
  ],
  "debug": { "noise_boxes": [...], "gated_out": [...], "detected_boxes": 27 }
}
```

### 三个要留意的字段语义

**`seats_ring_from_hero` 是座位环序，不是下注顺序。** 翻牌前 UTG 先动、翻牌后 SB 先动，
hero 未必是第一个行动的。字段名带 `from_hero` 就是为了说清这一点，别当行动顺序用。

**`status` 是三态，不把两件事混为一谈。** 「座位上没有人」（`empty`，`stack` 返回 0）
和「有人但筹码读不出来 / 显示为 0（全下、刚输光）」（`uncertain`，`stack` 为 `null`）
是两回事，都输出 0 的话下游分不清。这是**识别侧的判据**；接口要不要返回这个座位另说 ——
接口按 `in_hand`（有筹码才算在场）过滤，`uncertain` 的座位不返回，见响应语义。

**`position` 和 `is_dealer` 是两件事。** `position` 是给下游用的位置名（SB/BB/UTG/…/BTN），
`is_dealer` 是「界面上那个 D 圆片画在他旁边」的布局事实。绝大多数时候两者一致，
**单挑是例外**：按钮位下小盲，庄家的 `position` 是 `SB` 而不是 `BTN`，
「他是庄家」这件事由 `is_dealer: true` 表达。

**`dealer.status` 可以是 `not_found`。** 「9 席有且仅有 1 个庄位」是**牌局的事实**，
不是**检测器的能力**。找不到就报找不到，检出两个就报 `ambiguous`，绝不取最大响应蒙混过去。

---

## 设计要点

### 坐标一律归一化，不 resize 整图

配置里存 0–1 相对值，运行时乘实际宽高。**老 `rois.json` 的 `base_size=1920x1080`
与实际截图 1440×3200 不符，`scan.py` 的 `normalize()` 把图强行压扁了** ——
新管线不碰 `normalize()`，`reference_size` 只作溯源记录。

### 全图 OCR + 几何分配，而不是逐 ROI 裁剪

实测全图 det+rec 召回够用，而且免费拿到文本框坐标，比裁剪更抗小幅位移。
过滤分两层，都是几何/类型约束，**不用内容黑名单**（那种词表在 app 改版后必然失效）：

1. **区域门**：桌面区 `y ∈ [0.11, 0.855]`，一刀切掉状态栏和播放器 UI
2. **槽位门**：文本框中心必须靠近某个槽位（按槽位自身尺寸归一化的距离，不是等分扇区
   —— 实测座位角度不均匀，顶排两个只差 26.2°，其余 38–45°）
3. **类型门**：筹码必须解析成功且单位是 BB；名字要过「像名字」谓词
4. **一致性门**：名字框中心 y 必须小于同座位筹码框中心 y

被丢弃的框不扔，收进 `debug.noise_boxes` / `debug.gated_out` —— 配置和 app 版本
对不上时，这是第一手证据。

### 筹码强制要求 BB 单位（这是修的一个真 bug）

`scan.clean_number()` 有一条**静默错误路径**：`238BB` 若被读成 `2388`（BB→88），
此时字符串里已无字母，`_UNIT_RE` 匹配不到、`corrected` 也是 `False`，
于是它返回 `(2388, None, False)` 且 `ok=True` —— **自信地给出错值且零告警**。

比读不出来危险得多。所以 `fields.parse_chip()` 加了一道单位闸：单位对不上就一律
判不确定，不采信。实测：

```
238BB   -> 238      ok
2388    -> None     单位不是 BB（实际 无），值可能被误读，不采信
1.2K    -> None     单位不是 BB（实际 K）
```

### 名字为什么读不出来（三个 bug 的复盘）

`table1.jpg` 上曾经有四个名字读不出或读错（`唐小鱼呀`→`小鱼`、`Dragon333`→`ragon33`、
`菜菜子31` 和 `谈轩66` 整个丢失）。查下来是**三个独立的问题叠在一起**，
而且**全图 OCR 其实四个全读对了（0.996–0.998）**：

| 现象 | 根因 |
|---|---|
| `唐小鱼呀`→`小鱼`、`Dragon333`→`ragon33` | **名字槽太窄**。槽位宽度是当初从某一帧量出来的**文字紧框**（97–219px 不等），名字一长就被裁掉开头 |
| `菜菜子31`、`谈轩66` 整个丢失 | **名字谓词误杀**。那条「数字占比 > 1/3 就否决」的规则把带数字的名字全挡在门外（前者 2/5、后者 2/4） |
| （潜在）`菜菜子31菜菜子31` | **重复检测**。检测模型偶尔对同一段文字吐两个几乎重合的框，名字槽能装 2 个框，就会拼两遍 |

修法：

1. **名字槽改成「搜索区域」而不是「文字紧框」**：统一 280px 宽（约 6 个汉字），
   中心一律不变。宽到一个名字不可能超出。
2. **谓词改成「去掉数字后还剩不剩字」**：剩空 → 纯数字，不是名字；剩字母或汉字 → 是名字。
   数字占比仍有个很宽的兜底上限（2/3），只用来挡住 `A12345678` 这种。
3. **分配前去重**：文本相同且 IoU > 0.5 的框只留一个。

### 名字：检测结果说了算，阶梯只提分数

最根本的一处改动：**全图检测读出来的原文（seed）就是名字，阶梯不覆盖它。**

以前是拿槽位裁剪重新识别一遍，等于**把已经读对的结果扔掉再猜一遍** —— 槽位比名字窄时
就猜错。现在：

- 有检测结果 → 文本以它为准；阶梯只在读出**同样文本**时把分数顶上去，
  读出不同文本时只用来标记「有分歧，值得看一眼」
- 没有检测结果（检测漏框了，但筹码读出来了说明有人）→ 才真的靠阶梯识字，
  这时用加宽后的槽位裁剪

**为什么不让阶梯改文本**，三条实测教训：

1. 全图检测本来就准，四个「读不出来」的名字它全读对了。
2. 阶梯从来没真正修正过一个错字，只在已正确的文本上抬分数
   （`悍将v1` 0.896→0.982，文本没变），却制造了新错误。
3. 阶梯对裁剪边距极其敏感，而且**高分不等于读对**：留白 8% 时 `稚气未酒` 被切成
   `稚气未`，那个错读法置信度 **0.998**，反而盖过检测的 0.905。**按分数选就会选出错的。**

改完之后，名字文本**完全不受裁剪边距影响**（实测 0.05–0.35 五档全部 18/18），
不再有一个需要精调的脆弱参数。

### 名字用「重试阶梯」，不用投票

4 级阶梯（原始 / 放大3x / 放大3x灰度 / 放大5x二值），**取分数最高的一级**。

之所以不投票：`洒/酒` 这类形近字错误是识别模型的**系统性**先验错误，不是随机抖动 ——
所有预处理变体都会一致地输出同一个错字。投票会把 4:1 的高一致性错误答案包装成
看起来很确定的结论，**一致性在系统性错误上和正确性是负相关的**。

阶梯本身是有用的：`悍将v1` 从 L0 的 0.896 提升到 L3 的 0.983。

玩家池是陌生人、没法做白名单，所以剩下的手段是「让错得便宜且显眼」：
`name_score` + `name_review` + `name_variants` + `--dump-seats` 出的接触表，
人眼扫一遍约 10 秒。

`name_review` 的判据是「**分数接近**却读得不一样」。早期版本是不管分数、只要各级文本
不一致就报复核，结果被阶梯噪声误触发（放大/二值那几级常多吐一个偏旁或漏字，
分数只有 0.73–0.93，而检测结果 0.98+ 是对的）—— 拿明显更差的读法去质疑明显更好的读法，
是假警报。

### 庄位怎么找

庄位是**白底圆形 + 深色「D」**，画在庄家头像旁边，**归给最近的头像**。
三重过滤，每重都能独立挡掉一类干扰：

1. **区域门** —— 挡掉状态栏/播放器里的白色元素
2. **几何门** —— HSV 近白低饱和掩码 → 面积 + 圆度 + 长宽比
3. **识别门** —— 认出「D」才算数（`reads_d: true`）

裁剪识别**必须贴合外接框**，只留约 15% 边。按 `area_max` 固定裁一大块会失败：
四周全是毛毡时白圆片缩成一个小点，识别器会把它读成「·」而不是「D」——
实测差距是 0.97 和 0（读不出）。这个坑踩过，别再改回去。

归座用**到头像外接框的矩形距离**，不是到中心的距离。D 贴在头像边上，
矩形距离更贴合直觉，实测区分度也更好：庄家 0.61 个头像宽，最近的邻居 1.9，差 3 倍。

阈值 `max_seat_dist = 1.2`，卡在两者中间。超出就当作误检丢掉，不硬塞给某个座位。

结果状态：`found` / `not_found` / `ambiguous`。**检出多个候选时报 `ambiguous` 不猜**
（正常牌局只有一个 D，多出来的一定是误检或画面异常）。

### 位置怎么算

找到庄位后，**从庄位左边开始沿行动方向走**，逐个套位置名：

```
庄位左边第一个 = SB，第二个 = BB，…，绕回来最后一个 = BTN（庄家自己）
```

这份表和 call-back `utils/hand.go` 的 `positionsByTableSize` **是同一份契约**
（`poker_ocr/positions.py` 是它的 Python 镜像，`tests/test_positions.py` 里有一道
防漂移的断言）。契约的要点是：**每个位置列表都是按翻前行动顺序排的**
（SB 先说话、BTN 最后），所以从庄位左边起沿行动方向走正好逐个对上。

- **桌号 = 有筹码座位的数量**（`in_hand`），空座位不发牌、不占位置。9 人桌空了 2 个座位
  就是 7 人桌，位置表换成 `SB/BB/UTG/LJ/HJ/CO/BTN`，不在场座位的 `position` 为 `null`。
  丢掉的空位不会让剩下的人整体错位：位置是**从庄位起在在场的人里**发的，
  不是按 9 个座位槽算的。
- **庄家退出 / 暂离（死按钮）**：按钮可能还留在空座上。这时不再把整帧判成
  「庄位不在环上」丢掉，而是按固定座位环从按钮下家继续发位置：按钮下家仍是
  SB、再下家 BB，按钮仍然占一个位置，所以这一手最后一个在场的人拿 CO（没有
  真正的 BTN 玩家）。其余人的相对位置不会因为按钮位上没人而整体前移。
- **单挑（2 人）是唯一的例外**：按钮位下小盲，庄家自己就是 SB。通用公式会把庄家算成
  BB，所以代码里单独处理。位置名仍写 `SB`（契约里 2 人桌只有 SB/BB 两个合法值），
  「他是庄家」由 `is_dealer` 表达。
- 环的起点是任意的。`--hero-seat` 把环旋转到 hero 开头，不影响位置结果 ——
  单元测试里专门盯着这条（`test_ring_rotation_is_irrelevant`）。

---

## 尚未验证的假设（重要）

### 1. 行动方向 `action_direction` ✅ 已由盲注证实

**行动顺序本身不记录**（按需求暂缓），但行动**方向**是位置推导的前提，已经验证过了。

证据在 `table-wrap-d.jpg`：那帧是**翻牌前**——庄位是 hero，屏幕上顺时针紧邻庄位的一侧
坐着 **无敌猪肉佬 0.5BB（SB）**、再下一个是 **陈凤 1BB（BB）**，
且 `0.5 + 1 + 4.5 = 6BB` 正好等于画面显示的总底池。盲注位置是牌局定义，不是启发式，
据此**反推出行动方向就是屏幕顺时针**，也就是配置里的 `action_direction: "cw"`，
`direction_verified` 已置为 `true`。

数据只有这一帧，但盲注的方向性是定义级别的，不是统计推断，所以足够。万一将来发现反了，
后果是位置整体镜像（SB 和 CO 对调），改配置里 `cw` → `ccw` 一个词即可。

### 2. 庄位检测只在一帧上验证过

白底 D 圆片的检测已实现并跑通（见下文「庄位怎么找」），但**只在一帧
（`table-wrap-d.jpg`）上验证过**。三重过滤的阈值都是按那一帧量的：

- 真 D：50×50、area≈1894、圆度 0.900
- 干扰项：下注筹码 68–73px、area≈4700（太大）；文字笔画 20–25px、area 500–700（太小）

尺寸差一个量级，所以面积区间卡得死也够稳。但**如果 app 改版换了 D 的尺寸，
或者换个分辨率导致 D 的像素直径变了，这些阈值就会失准** —— 那时调
`config/table_default.json` 里的 `dealer.blob.area_min/area_max` 即可。
`--dealer-seat` 手动指定永远优先，也是自动检测报 `ambiguous` 时的兜底。

没做模板匹配（`cv2.matchTemplate`）：它对缩放、抗锯齿、毛毡渐变背景都敏感，
换个机型密度档位就可能失效。而「近白低饱和的圆片」这个特征跨机型稳定得多。

### 3. 空座位的阈值是估计值（已有实测样张）


占用判定的两个视觉信号阈值（头像区 std ≥ 18、筹码区 std ≥ 25）当初是**估计值**。
`table-8p.jpg`（右下角一个「+」空座位）实测：空座位的头像区 std 21.4、筹码区 5.6，
在场玩家头像区 31.7–62.9、筹码区 59.5–73.2 —— 筹码区分离得很干净，头像区是 21.4 越过
18 这条线才被判成 `uncertain`，也就是当时那张「多算一个人」的根因。

**现在它不再影响结果**：入局判据改成「筹码读出来」（见响应语义），
空座位和 `uncertain` 一样都按空座位处理、都不返回，所以阈值只决定 `status` 这个标签，
以及提醒里怎么措辞。真要再收紧就调 `avatar_var_min`（实测分界在 21.4 与 31.7 之间），
调完用 `tests/golden/table-8p.expected.json` 兜底。

### 4. 跨机型

当前只在 1440×3200 上验证过。`profile.detect_canvas()` 会在宽高比和参考尺寸明显不符时
去找 letterbox 黑边，但**没在真机上验证过**。如果你只在固定分辨率下截图，这段代码不会触发。

---

## 文件结构

```
api.py                    # HTTP 接口（uvicorn api:app）
extract_table.py          # CLI 入口
rois.json                 # 识别结果（每次 CLI run 自动写）
config/table_default.json # 归一化槽位 + 环序 + hero + 方向 + 阈值
poker_ocr/
  profile.py              # 配置加载/校验、归一化→像素、环序几何断言
  ocr.py                  # OcrBox + 带框识别
  layout.py               # 文本框→槽位分配
  fields.py               # 筹码解析（BB 单位闸）、名字谓词、重试阶梯
  dealer.py               # 庄位检测：手动 + 白底 D 圆片自动检测
  positions.py            # 庄位 → 位置名（SB/BB/UTG/…/BTN）
  pipeline.py             # 单帧 → FrameResult（含占用三态、位置）
  cards.py                # 公共牌 / hero 手牌：颜色+形状判花色，字形模板判点数
  assets/card_rank_templates.png  # 13 个点数的字形模板（工具生成，勿手改）
tests/
  run_all.py              # 跑全部测试
  test_positions.py       # 位置推导单元测试
  test_dealer.py          # 庄位检测单元测试（合成图）
  test_cards.py           # 公共牌 / hero 手牌回归（images/card 的 16 个标注样本）
  test_stable_cards.py    # 跨帧牌面稳定闸（回退/张数/牌背/2 帧确认/多帧投票）
  test_doubt_window.py    # 疑惑帧时间窗仲裁（低置信度/牌背/无多数/提前解除）
  test_export_live_hands.py  # 从 live.states.jsonl 导出完整手牌
  test_compare_advice_styles.py # 回放对比脚本的筛选/决策点/对比口径
  test_card_replay.py     # 真实样张回放：一手牌只落一条记录、牌面只前进
  test_extract.py         # 端到端回归测试
  test_api.py             # HTTP 接口测试
  golden/*.expected.json
tools/
  export_live_hands.py    # 从 live.states.jsonl 筛出完整手牌 → hands.json
  compare_advice_styles.py # 大幅盈亏手牌回放，对比 lean / skills 提示词
  build_card_templates.py # 从标注样本重建点数模板图集
  card_probe.py           # 单图读牌 CLI
scan.py                   # 旧的第 0 步验证脚本，已冻结
```

**`rois.json` 的含义变了。** 它以前是 `scan.py` 的 ROI 配置（`base_size` + 3 个字段坐标），
现在改作**识别结果文件**。几何配置已经搬到 `config/table_default.json`，旧坐标是可重建的。

连带影响：`scan.py run` 会因为读不到 `fields` 而报错退出（它自己有兜底提示，不会崩）；
`scan.py calibrate` 不受影响（读不懂旧文件时会当作空白从头开始）。要恢复旧的用法，
用 `extract_table.py run --out 别处.json`，别让结果覆盖 `rois.json`。

`scan.py` 保留作为标定工具，它没有被废弃的部分（`clean_number`、`preprocess`、
`RapidEngine`）仍被新管线 import 复用。它的 `_flatten()` 只吐 `(text, score)` 二元组，
**刻意没有改** —— 改了会波及它自己的 ROI 路径，新管线另写了带框的解析。

---

## 已知的坑

- **`base_size` 那套已经不用了。** 配置里出现 `reference_size` 只是记录来源，
  它**不参与运算**。任何时候都不要把整图 resize 到某个基准尺寸。
- **`scan.py` 的 `clean_number` 单独用仍有静默错值风险**（BB→88）。新管线走
  `fields.parse_chip` 的单位闸绕开了它，但如果你直接调 `scan.py`，那条路径还在。
- **名字的 `name_score` 低于 `name_min_score`（默认 0.7）或各级结果不一致时会打
  `name_review: true`。** 这是给人看的信号，不是自动纠正。
- **同名不同人**：名字只做字符串输出，不做身份识别。两次截图里的 `陈凤` 是不是同一个人，
  这个模块不判断。
