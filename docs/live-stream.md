# 实时推流识别（OBS → MediaMTX → poker-ocr）

把手机牌桌画面通过 OBS 推到本机 MediaMTX，按固定间隔采样、识别成结构化
JSONL。本文是**当前方案的完整说明**：架构、功能、标定流程、启动命令、输出格式
和排障。

---

## 1. 方案总览

```
手机 / 采集卡
   │  AirPlay / scrcpy / 采集
   ▼
OBS（画布/输出保持手机比例，不拉伸）
   │  SRT publish :8890/udp
   ▼
MediaMTX（SRT 接收 → RTSP 127.0.0.1:8554）
   │  RTSP read（只保留最新帧）
   ▼
poker_ocr.live
   ├─ 裁黑边（auto）
   ├─ 宽高比对齐（AspectFit）
   ├─ 固定 ROI：名字 / 筹码 / 下注 / 动作 / 庄位 / 底池 / 盲注
   ├─ ROI 去重（没变的块不重复跑 rec）
   ├─ 状态机（防抖、丢弃、跨帧继承）
   └─ logs/live.states.jsonl
```

### 两条识别路线

| 路线 | 参数 | 适用场景 | 速度 |
|---|---|---|---|
| **ROI（默认）** | `--recognizer roi` | 布局固定的 App（WPK 竖屏等） | ~0.5s/帧 |
| **全图** | `--recognizer full` | 浏览器牌桌、布局不固定 | 0.5–1.5s/帧 |

ROI 路线只裁固定槽位、只跑 rec，不跑全图检测，是实时链路的主力。
全图路线做 det+rec 后按几何位置聚类座位，不依赖固定布局，作为兜底。

### 关键设计

- **只保留最新帧**：reader 线程持续 `read()`，邮箱里永远只有一帧；处理不过来
  就覆盖旧帧，延迟不会越积越大。
- **连续动作合并（动作区静止闸）**：连续弃牌/下注时，中间帧的徽章还在动画里，
  逐帧识别只会写出一串「弃牌 1 个 → 2 个 → 3 个」的半成品记录。识别之前先看
  **动作区**（名字/筹码/下注/头像中心/底池/牌面）的像素变化：还在变就丢掉当前
  帧继续等；静止 `--settle` 秒（默认 0.25）后才把邮箱里**最新的一帧**交给识别。
  动作一直不停时 `--settle-max`（默认 1.5s）兜底。盲注横幅的倒计时、头像边缘
  的计时环不参与判据，否则画面永远不会「静止」。`--settle 0` 关闭。
  仅 roi 路线：区域来自固定槽位，full 路线没有布局可用。
- **帧间串行、帧内并行**：reader 只解码，`StateStream.ingest()` 仍只在一个
  处理线程里调用；同一帧里未命中缓存的 ROI rec 由线程池并行（`--roi-threads`，
  默认 4，`1`=串行）。共享同一个 OcrEngine/ONNX session，实测并发结果与串行
  逐字一致（M2：39 块全跑 475ms → 4 线程 282ms，约 1.7×）。
- **严格质量闸**：一帧里任何一个字段无法精准识别，整帧丢弃（`discard`），
  不把猜测写进输出。
- **ROI 相对位置**：所有槽位都是 0–1 归一化坐标。画面宽高比和 profile 参考
  不一致时，先按参考比例重采样（`AspectFit`），保证 x/y 共用一个像素缩放。
- **ROI 级去重**：同一块 ROI 和上一帧逐 bit 相同 / MAD ≤ 0.2 时复用上一帧读数。
- **一手牌内的语义缓存**：名字、盲注横幅、庄位在一手牌里不会变，读到可信值后
  直接钉住，不再逐帧 OCR；换手时（公共牌掉回 0 / hero 换两张牌 / 庄位轮转，
  外加每 2s 兜底复核）统一解钉重新识别。盲注横幅里的房间号和倒计时每秒都在
  变，按像素判据本来会每帧 miss。该缓存只在 live 传 `extra_signature`
  （手牌/公共牌）时启用；离线工具没有可靠换手边界，保持纯像素去重。
  实测连续牌局帧均值 ~723ms → ~482ms（p50 497ms → 297ms）。
- **庄位识别不逐帧重跑**：白底「D」圆片的 OCR 结果按「位置 + 裁剪图指纹」
  缓存，一手牌内只有按钮挪动（像素变了）才重读；第一档（原尺寸 raw）以
  0.9 以上的分数认出 D 时也不再跑后两档重试。整帧庄位检测（blob 扫描）
  也在同一手内跳过，只做 2s 一次复核。实测庄位检测从 150–480ms/帧
  降到 ~50–100ms/帧（首次）/ ~20–40ms/帧（缓存命中）/ ~0ms（整帧复用）。
- **断流重置**：重连后清空 ROI 缓存和状态机，下一帧重新当基线；累计计数保留。

---

## 2. 功能

### 每帧识别出的状态

- **座位**：9 人桌（ROI profile）或 2–9 人（全图路线）
- **每个玩家**：名字、后手筹码（BB）、本街下注、动作
  （`check/call/bet/raise/fold`）、是否弃牌、是否在池、是否全下
- **位置**：BTN / SB / BB / UTG / …（由庄位和在场人数推导）
- **庄位**：白底圆形「D」检测，归给最近头像
- **全局**：盲注级别、底池、上一街底池、本街下注总额
- **回合**：是否轮到 hero（读第一个下注预设钮的 BB 值行）
- **桌型 / 在池人数**

### 工程能力

- 黑边自动裁剪；非等比画面按参考比例还原
- 低分辨率自动放大后做庄位 blob 检测（短边 < 600px，最多 3x）
- hero 筹码胶囊双槽位：有手牌时在最底部，无手牌时在头像下方（`chip_slot_alt`）
- 手势/弹窗/动画帧自动丢弃，不污染状态
- 同一状态连续 `--stable-frames` 帧才记录；名字连续 `--name-confirm` 帧才更换
- 盲注读不到时继承上一帧（`blinds.source = "carried"`）
- 断流自动重连，JSONL 用 `epoch` 区分会话

### AI 实时建议（`--ai-advice`，可选）

- **触发**：`hero_turn` 为真（默认 `--ai-confirm 1`，第一帧就触发），且这个
  决策点的指纹（行动条数 / 底池 / 最高下注）没建议过。多人池里 hero 过牌、
  对手下注、又轮回 hero 时会给第二次建议。
- **「轮到我」判据**：hero 头像槽被**蓝色「下注/加注」圆钮**覆盖
  （`images/roi/hero_bet.jpg` 实测蓝色占比 0.85–0.86，非回合帧 0.000–0.003）。
  老 UI 没有蓝色按钮时回退到 `hero_turn_probe`（预设钮的 BB 值行）文本判据。
- **行动线**：吃**每一帧被接受的状态**（不只 JSONL 里被记录的）。动作徽章与
  轮到我都被排除在变化指纹之外，只吃 `state` 记录会漏掉大量过牌/跟注。
  每座每街按「徽章 + 本街下注额」落一条，重复帧合并。
- **街末补漏**：换街时用底池账单对账 —— `pbs(下一街) − pot_start(上一街)` 是
  这一街所有人实际投进去的钱；如果它比行动线记录的总额多，先按已记录水位补
  跟注；对不上再用剩余人数反解最终水位，补「一次加注 + 跟注」（落库时带
  `inferred: true`）。金额是底池精确反解出来的，但加注者身份只能按行动顺序取
  「最后一个已记录动作之后最早能行动的人」，无法由底池唯一确定。专门覆盖最后
  位置预设动作、视频流没抓到那一帧就换街的情况；两种模型都对不上账就不猜。
- **技能**：只加载**当前行动街**的技能（与 call-back 的
  `services/skills/*.md` 正文逐字一致），常驻 `core-stance` + `odds-table`，
  再按翻前形态 / 牌力分档 / 听牌 / 多人池 / cbet 点路由专项技能。
- **选项**：翻后是弃牌、跟注（没人下注时是过牌）、加注到底池 1/3、1/2、2/3、1、
  1.2 倍；翻前换成 bb 口径 —— 平跟/过牌 + 开池到 2.5~4BB（按位置档、前注×2、
  每个溜入者 +1BB），面对加注时是 3×/4× 上家加注。
  金额由程序算好（翻后口径为「当前最高下注 + f ×（底池 + 需跟额）」），低于最小
  加注额会抬到合法值，超过后手会封顶为全下；模型只返回选项 id。
- **执行**：模型调用在后台线程，单次超时 `--ai-timeout`。生成建议期间主循环
  **暂停识别新帧**（新帧只丢弃），结果返回后直接处理邮箱里的最新帧 —— 等待期间
  画面只会越来越旧，继续 OCR 既浪费 CPU 也会让下一步决策基于过时画面。
  暂停期间每秒打一行 `[建议] 生成中… Ns` 心跳（实测 JEV 一次 1.7–3.4s），
  避免控制台先静默、再把建议和恢复后的识别日志一次性吐出来；建议行同时带上
  本次耗时，慢在模型还是识别一眼可见。失败时回退到最保守动作并记录原因。
- **连接复用**：同一 host 的 HTTP/1.1 长连接会缓存下来，省掉每次建议的 TLS
  握手（国内到 OpenRouter 实测 0.4–1.4s，常常比模型本身还慢）。配置了
  `HTTPS_PROXY` 等代理变量时退回 urllib 的默认行为，不绕过代理。
- **凭据**：读 `AI_SERVER_API_KEY / AI_SERVER_BASE_URL / AI_SERVER_MODEL`。
  写在项目根目录的 `.env`（模板 `.env.example`）或直接设为环境变量，环境变量优先。
  `.env` 找不到时退到启动 CWD；`ENV_FILE` 可显式指定路径（读不到直接启动失败）。
  启动日志会打印命中的 `.env` 路径与凭据来源，但**不打印 Key**。
- **协议按模型名选**：名字里带 `jev`（`jev-1.14` / `typesafe/jev-1.14`）走
  OpenRouter 的 decisions 接口 —— `POST AI_SERVER_BASE_URL`，请求体是
  `{"model", "state", "questions"}`，候选动作变成一道 `choice` 题
  （criteria 的 key 是选项 id），从 `answers.option` 取选择与概率。
  其他模型走 OpenAI 兼容的 `{BASE_URL}/chat/completions`。
  两套协议对下游输出相同的 `{option, reason}`。
  `AI_SERVER_REFERER` / `AI_SERVER_TITLE` 是 OpenRouter 的可选排名头。

### 工具链（抓帧 → 裁剪 → 验证 → 重新标注）

| 工具 | 作用 |
|---|---|
| `tools/capture_stream.py` | 从 RTSP 按 0.5s 抓帧存图 |
| `tools/crop_frames.py` | 批量裁黑边 + 按参考比例对齐 |
| `tools/validate_roi.py` | 用现有 profile 跑 ROI，输出叠加图 + JSON 报告 |
| `tools/refit_profile.py` | 用真实帧重新标注槽位，生成新 profile |
| `tools/calibrate_live.py` | 从实时流跑一次全图识别，直接生成 runtime profile |
| `tools/video_to_states.py` | 离线视频 → 状态 JSON（不经过 OBS/RTSP） |

---

## 3. 快速开始

### 3.1 安装 MediaMTX

```bash
brew install mediamtx
```

OpenCV 的 wheel 已自带 FFmpeg，不需要额外安装。

### 3.2 启动 MediaMTX

先改 `config/mediamtx.yml` 里的 `srtPublishPassphrase`（10–79 位），
或者启动时用环境变量覆盖：

```bash
cd ~/codes/poker-ocr
mediamtx --validate-conf=config/mediamtx.yml
mediamtx config/mediamtx.yml
```

正常日志：

```
[SRT] listener opened on :8890 (UDP)
[RTSP] listener opened on 127.0.0.1:8554
```

确认端口：

```bash
lsof -nP -iUDP:8890
lsof -nP -iTCP:8554
```

首次运行 macOS 会询问是否允许 MediaMTX 接受入站连接 —— 允许 UDP 8890。

### 3.3 OBS 设置

**Settings → Stream**

- Service：`Custom`
- Server：
  ```
  srt://<Mac 局域网 IP>:8890?streamid=publish:live&passphrase=<口令>&pbkeylen=16
  ```
- Stream Key：留空

> `pbkeylen=16` 必须带。SRT 的 `latency` 单位是**微秒**，不写默认约 120ms。
> 局域网可信时也可以清空 passphrase，去掉 URL 里的加密参数。

**Settings → Output（Advanced）**

- Encoder：`x264`（Apple Silicon 可用 Apple VT 低延迟预设）
- Rate Control：`CBR`，Bitrate `4000 Kbps`
- Keyframe Interval：`1s`
- x264 Options：`tune=zerolatency bframes=0`
- 音频：关闭

**Settings → Video**

- Base / Output Resolution：和手机画面同比例（推荐 `1080x2400`）
- FPS：`15`
- 缩放方式：**Fit / 适应屏幕，不要拉伸**

**Settings → Advanced**

- Color Format：`NV12`
- Color Space：`Rec. 709`
- Color Range：`Full`

> 不要缩放、不要加黑边/填充。分辨率越低，筹码数字和庄位 D 越难读；
> 建议 OBS 输出尽量接近 `1080x2400`。

点击 **Start Streaming**，MediaMTX 日志出现：

```
[SRT] [conn ...] is publishing to path 'live'
```

### 3.4 降低 OBS → Python 的传输延迟

这条链路是 **OBS 采集 → 编码 → SRT → MediaMTX → RTSP → FFmpeg 解码**，
每一跳都有缓冲。按收益从大到小：

**① OBS 和 worker 在同一台 Mac 时，直接用虚拟摄像头（推荐）**

```
OBS（Start Virtual Camera）→ Python 直接读摄像头
```

完全跳过 编码 / SRT / MediaMTX / RTSP / 解码，少 0.3–0.8s。
OpenCV 的 AVFoundation 后端能直接打开它：

```bash
# 1) 在 OBS 里点 Start Virtual Camera
# 2) 查索引（分辨率等于 OBS 画布的那一项）
.venv/bin/python tools/list_cameras.py

# 3) 用 cam:N 启动，不再需要 MediaMTX
.venv/bin/python -m poker_ocr.live \
  --url cam:1 --profile config/table_wpk_live.json \
  --initial-blinds 1/2 --out logs/live.states.jsonl
```

注意：虚拟摄像头输出的是 OBS **画布**分辨率/帧率（可能是 30/60fps），
比 RTSP 的 10fps 更费 CPU；worker 只保留最新帧，识别节奏不受影响。
macOS 首次使用要允许终端访问相机。

**macOS 的坑：虚拟摄像头固定 1920x1080。** 直接把 1080x2400 的竖屏画布塞进
16:9，牌桌只会剩 ~486x1080（参考尺寸的 49%，低于 0.5 告警线），筹码数字会
读不出来（实测 5 帧全部因 `pot/dealer/stack` 丢弃）。解决办法是**横屏画布 +
旋转源**：

1. OBS 设置 → 视频：Base / Output 都设 `1920x1080`；
2. 手机画面源右键 → Transform → Rotate 90°（顺/逆都行），缩放到宽度约 1920
   （高度约 864），居中；
3. Start Virtual Camera，然后：

```bash
# OBS 顺时针转了 90° → worker 用 270 转回来；逆时针转则用 90
.venv/bin/python -m poker_ocr.live --url cam:1 --rotate 270 ...
```

这样有效内容是 864x1920，约参考尺寸的 **88%**，足够 OCR。
方向不确定时先 `--dump-frames` 看一帧：牌桌上下颠倒就把 90/270 换一下。
自动裁剪会把画布上的黑边切掉。

**② 继续走 SRT 时，压低 SRT 延迟**

在 OBS 的 Server URL 末尾加 `&latency=20000`（单位**微秒**，即 20ms）：

```
srt://<Mac IP>:8890?streamid=publish:live&passphrase=<口令>&pbkeylen=16&latency=20000
```

局域网 RTT 远小于 20ms。SRT 的延迟是两端**协商取较大值**，如果 MediaMTX
这一侧的下限更高，就压不到 20ms —— 用下面的方法量一次再决定是否值得。

**③ MediaMTX 出口队列**

`config/mediamtx.yml` 已把 `writeQueueSize` 从默认 512 降到 **128**：
读者偶尔卡顿（OCR 抢 CPU）时，最多只攒约 0.3–0.4s 的包，恢复后不会先播一两秒旧帧。
改完要重启 MediaMTX。

**④ FFmpeg 侧**

`OPENCV_FFMPEG_CAPTURE_OPTIONS` 已经带了
`fflags=nobuffer flags=low_delay max_delay=0`；实测 `grab()` 按源的 10fps
节奏返回，`retrieve()` 只要 3–5ms，Python 侧不是瓶颈。
**不要**再盲目加 `threads=1` 或 `analyzeduration=0/probesize=32` ——
在本机实测前者让首帧等了 20s，后者直接解不出画面。

**怎么量端到端延迟**

```bash
# 1) OBS 浏览器源加载 docs/live-clock.html，放在画面顶部
# 2) 启动 worker 并保存采样帧
.venv/bin/python -m poker_ocr.live --dump-frames /tmp/liveframes
# 3) 对比 dump 帧文件名的墙钟时间和画面里的时钟
```

日志里的 `queue_ms` 只是「帧读出来 → 开始 OCR」的排队时间，**不含**
OBS/SRT/RTSP，所以它很小不代表端到端不慢。

### 3.5 启动识别

```bash
cd ~/codes/poker-ocr

# 先跑 10 帧冒烟
.venv/bin/python -m poker_ocr.live --max-frames 10

# 正式运行（Ctrl-C 结束，自动写 session_end）
.venv/bin/python -m poker_ocr.live \
  --url rtsp://127.0.0.1:8554/live \
  --profile config/table_wpk_1080x2400.json \
  --every 0.5 \
  --out logs/live.states.jsonl
```

启动顺序无所谓：worker 连不上会每 1s 重试。

---

## 4. 针对当前流的标定流程（推荐）

同一台设备、同一套 OBS 参数下，建议先抓一批真实帧重新标定 ROI。
这是目前效果最好的流程。

### 4.1 抓帧（每 0.5s 一张）

```bash
# 抓 200 张（约 100 秒）
.venv/bin/python tools/capture_stream.py \
  --every 0.5 --max-frames 200 --out logs/live_frames
```

- 只拉流、不加载 OCR；持续 `grab()` 丢弃旧缓冲，到点再 `retrieve()`。
- 文件名 = 墙钟时间 + 毫秒 + 序号，字典序即时间序。
- 断流自动重连。

### 4.2 裁黑边 + 对齐比例

```bash
.venv/bin/python tools/crop_frames.py logs/live_frames \
  --out logs/live_frames_cropped
```

输出和实时 worker 看到的画面完全一致：`plan_crop(auto)` + `AspectFit`。

### 4.3 用现有 profile 验证

```bash
.venv/bin/python tools/validate_roi.py logs/live_frames_cropped \
  --profile config/table_wpk_1080x2400.json \
  --initial-blinds 1/2 \
  --out-dir logs/roi_validation
```

产物：

- `<文件名>.roi.jpg`：3 倍放大，画上每块 ROI 和识别文本 —— **先看框有没有偏**
- `report.json`：每帧的 `rect/text/score`、解析后的状态和 `errors`
- stdout：字段读取率、丢弃原因汇总

### 4.4 重新标注 ROI

```bash
.venv/bin/python tools/refit_profile.py logs/live_frames_cropped \
  --profile config/table_wpk_1080x2400.json \
  --out config/table_wpk_live.json --step 2
```

做法：

1. 每帧跑全图 det+rec，拿到所有文本框；
2. 按字段类型（名字 / 金额 / 盲注）匹配回原槽位；
3. 多帧取坐标**中位数**，只修正槽位整体偏移，保留原搜索区尺寸；
4. 命中不足 `--min-hits 5` 的槽位保持原值；
5. 同步标定庄位 blob 的亮度门槛（`v_min=170 / s_max=60`）。

产物：新 profile + `logs/refit_report.json`（每个槽位的命中数、旧值、新值）。

### 4.5 用新 profile 运行

```bash
.venv/bin/python -m poker_ocr.live \
  --profile config/table_wpk_live.json \
  --initial-blinds 1/2 \
  --out logs/live.states.jsonl
```

### 4.6 实测效果（2026-10-19，200 帧 / 982×2182）

| 指标 | 旧 `table_wpk_1080x2400` | 新 `table_wpk_live` |
|---|---|---|
| 状态可用 | 70/200 | **126/200** |
| 庄位检出 | 92/200 | **183/200** |
| 名字读取 | 96.1% | 95.9% |
| 筹码读取 | 97.2% | 97.3% |
| 底池读取 | 95.0% | 95.0% |
| 盲注读取 | 98.5% | 98.5% |

剩余丢弃主要来自游戏自身的瞬时状态：玩家资料弹窗、摊牌亮手牌顶掉筹码、
底池横幅只显示「总底池」标签、全下玩家筹码显示成裸 `0`。
这些帧本来就无法精准识别，按规则整帧丢弃。

---

## 5. 启动命令汇总

### 实时 ROI（推荐）

```bash
.venv/bin/python -m poker_ocr.live \
  --url rtsp://127.0.0.1:8554/live \
  --profile config/table_wpk_live.json \
  --initial-blinds 1/2 \
  --every 0.5 \
  --out logs/live.states.jsonl
```

### 实时 ROI（使用通用 1080x2400 profile）

```bash
.venv/bin/python -m poker_ocr.live \
  --profile config/table_wpk_1080x2400.json \
  --out logs/live.states.jsonl
```

### 全图标定 → 运行时 ROI（浏览器 / 布局不固定）

```bash
# 1) 标定一次
.venv/bin/python tools/calibrate_live.py \
  --url rtsp://127.0.0.1:8554/live \
  --frames 5 --crop auto \
  --out config/runtime_browser.json

# 2) 稳定运行
.venv/bin/python -m poker_ocr.live \
  --recognizer roi \
  --profile config/runtime_browser.json \
  --crop auto \
  --initial-blinds 1/2 \
  --lenient-chips \
  --out logs/live.states.jsonl
```

### 全图路线（不依赖固定槽位）

```bash
.venv/bin/python -m poker_ocr.live \
  --recognizer full \
  --full-config config/full_browser.json \
  --out logs/live.states.jsonl
```

### 离线视频

```bash
.venv/bin/python tools/video_to_states.py images/video/video1.mp4 \
  --every 0.5 --out /tmp/video1.states.json
```

### 冒烟 / 调试

```bash
# 只处理 10 帧
.venv/bin/python -m poker_ocr.live --max-frames 10

# 保存每个采样帧
.venv/bin/python -m poker_ocr.live --dump-frames /tmp/liveframes

# 关掉 ROI 去重
.venv/bin/python -m poker_ocr.live --no-dedup

# 不读头像（省 9 块 ROI，失去弃牌检测）
.venv/bin/python -m poker_ocr.live --no-fold
```

---

## 6. 输出 JSONL

每行一个完整 JSON，立即 flush。共 6 种记录：

| `type` | 时机 |
|---|---|
| `session_start` | 启动时一条 |
| `stream_info` | 分辨率 / 宽高比变化时一条 |
| `state` | 产生新状态时（防抖后） |
| `hand` | 换手时一条：上一手完整的逐街行动线（仅 `--ai-advice`） |
| `advice` | AI 建议完成时一条（仅 `--ai-advice`） |
| `session_end` | 退出时一条（含汇总） |

### 6.1 `session_start`

```json
{"type":"session_start","started_at":"2026-10-19T21:30:53.441+08:00",
 "url":"rtsp://127.0.0.1:8554/live","profile":"table_wpk_live.json",
 "recognizer":"roi","full_config":null,"every":0.5,"resolution":null}
```

### 6.2 `stream_info`

```json
{"type":"stream_info","ts":"...","recognizer":"roi",
 "resolution":[982,2182],"aspect":0.450046,
 "reference_aspect":0.450046,
 "crop":{"mode":"auto","src":[982,2182],"out":[982,2182]},
 "roi_fit":{"src":[982,2182],"out":[982,2182],"aspect":0.450046,
            "reference":[982,2182],"reference_aspect":0.450046,
            "deviation":0.0,"scale":[1.0,1.0]}}
```

- `resolution`：流原始分辨率
- `aspect`：流的宽高比
- `crop`：黑边裁剪结果
- `roi_fit`：按 profile 参考比例重采样的结果；`scale` 是参考图 → 实际 ROI 的
  x/y 缩放，正常应当相等。`scale < 0.5` 会额外告警分辨率不足。

### 6.3 `state`

```json
{"type":"state","seq":412,"epoch":1,"ts":"2026-10-19T21:30:31.321+08:00",
 "queue_ms":11.2,"proc_ms":561.4,
 "index":1,"t":36.73,"frame":"live_e1_000667_0036.73s",
 "table_seats":9,"players_in_hand":9,
 "blinds":{"sb":0.5,"bb":1.0,"ante":0.5,"raw":"1/2（1）521222","source":"frame"},
 "dealer":"midL","hero_seat_id":"hero","hero_turn":false,
 "pot":45.0,"pot_before_street":45.0,"street_bet_total":0.0,
 "players":[
   {"seat_id":"hero","position":"CO","name":"gggamel","stack":87.0,
    "bet":0.0,"action":null,"fold":false,"in_hand":true,
    "all_in":false,"status":"playing"}
 ]}
```

字段说明：

| 字段 | 含义 |
|---|---|
| `seq` / `epoch` | 全局帧号 / 第几次连接 |
| `ts` | 写入墙钟时间（ISO 8601，毫秒） |
| `queue_ms` | 帧被读出 → 开始 OCR 的排队时间 |
| `proc_ms` | 本帧 OCR + 状态机耗时 |
| `index` / `t` | 第几条记录 / 会话内秒数 |
| `frame` | 触发本记录的帧 ID |
| `table_seats` | 桌型座位数 |
| `players_in_hand` | 本手在池人数 |
| `blinds` | `sb/bb/ante` 单位为 BB；`source=frame` 或 `carried` |
| `dealer` | 庄位 `seat_id`；读不到时该帧会被丢弃 |
| `hero_turn` | `true/false/null`（null = 有东西盖住，不硬判） |
| `pot` | 底池（BB） |
| `pot_before_street` | 本街开始前已收进底池的部分（BB） |
| `street_bet_total` | `pot − pot_before_street` |
| `players[].stack` | 后手筹码（BB），不是带入 |
| `players[].bet` | 本街已下注（BB） |
| `players[].action` | `check/call/bet/raise/fold` |
| `players[].fold` | 弃牌检测，三态：`true/false/null` |
| `players[].status` | `playing/folded/buying_in/sitting_out/away` |
| `players[].notes` | 宽松解析等诊断信息（可选） |

### 6.4 `session_end`

```json
{"type":"session_end","ended_at":"...","summary":{
  "elapsed_seconds":53.8,"frames_read":1170,"processed_frames":63,
  "avg_proc_ms":526.9,"eff_hz":1.17,"reconnects":6,
  "sampled_frames":63,"accepted_frames":2,"discarded_frames":61,
  "discarded_reasons":{"dealer":60,"bet":13,"pot":21,"stack":180},
  "record_count":1,"roi_threads":4,"roi_reuse":{...},"recognizer":"roi"}}
```

### 6.5 `advice`

```json
{"type":"advice","ts":"...","hand_id":"h7","street":"flop",
 "hero_cards":["Ah","Kd"],"board":["Ks","7d","2c"],
 "pot":19.5,"to_call":8.0,
 "option":{"id":"raise-1/2","label":"加注到底池 1/2","kind":"raise",
           "amount_bb":25.75},
 "reason":"顶对顶踢被加注，牌面干燥，按成牌打法加注求价值",
 "source":"model","model":"deepseek-chat",
 "skill_codes":["core-stance","odds-table","opponent-read","postflop-made-hand"],
 "duration_ms":1840}
```

| 字段 | 含义 |
|---|---|
| `hand_id` | 第几手（换手时 +1） |
| `option.id` | 模型选的选项。翻后：`fold` / `call` / `raise-1/3` / `raise-1/2` / `raise-2/3` / `raise-1` / `raise-1.2`（没人下注时 `call` 表示**过牌**）；翻前：`fold` / `call` / `open-2.5`…`open-4` / `raise-3x` / `raise-4x` |
| `option.amount_bb` | 加注后的**本街总额**；跟注为需跟额 |
| `source` | `model` = 模型选的；`fallback` = 超时/报错/非法选项，按最保守动作 |
| `skill_codes` | 本次按当前街展开的技能 |
| `error` | 仅 `fallback` 时出现，说明原因 |
| `duration_ms` | 模型调用耗时（后台，不影响采样） |

### 6.6 `hand`

换手时写一条，内容是**上一手完整的逐街行动线 + 这一手给过的全部 AI 建议**
（对手与自己都记），这条记录就是一手牌的“手牌记忆”，可直接用于复盘。
退出时若最后一手还没换手，也会写一条并标 `"partial":true`。

```json
{"type":"hand","ts":"...","hand_id":"h6",
 "hero_cards":["Ah","Kd"],"hero_position":"BTN","table_size":6,
 "blinds":{"sb":0.5,"bb":1.0,"ante":null,"raw":"1/2"},
 "board":["Ks","7d","2c"],"pot":19.5,
 "streets":[
   {"street":"preflop","pot_start":0.0,"actions":[
     {"seat_id":"a","name":"老王","position":"CO","action":"raise",
      "amount":3.0,"all_in":false,"is_hero":false},
     {"seat_id":"hero","name":"me","position":"BTN","action":"call",
      "amount":3.0,"all_in":false,"is_hero":true}]},
   {"street":"flop","pot_start":6.0,"actions":[
     {"seat_id":"a","name":"老王","position":"SB","action":"bet",
      "amount":22.0,"all_in":false,"is_hero":false},
     {"seat_id":"hero","name":"me","position":"CO","action":"call",
      "amount":22.0,"all_in":false,"is_hero":true,"inferred":true}]}
 ],
 "advices":[
   {"hand_id":"h6","street":"flop","hero_cards":["Ah","Kd"],
    "board":["Ks","7d","2c"],"pot":19.5,"to_call":22.0,
    "option":{"id":"call","label":"Call 22BB","kind":"call"},
    "reason":"...","source":"model","model":"typesafe/jev-1.14",
    "skill_codes":["..."],"duration_ms":1840}
 ]}
```

`inferred: true` 只在**街末底池对账补出来**的记录上出现（正常抓到的记录没有这个键）。
`advices` 只在**这一手确实给过建议**时出现，按给出顺序排列；没有 AI 建议时连键都不出现。
其余 `advice` 记录仍会照常单独写一份，方便实时流式查看；复盘用 `hand.advices` 即可，不必再按 `hand_id` join。

### 6.7 丢弃语义

`state` 记录只在**所有字段都可精准识别**时产生。以下情况整帧丢弃：

| `discarded_reasons` | 典型原因 |
|---|---|
| `dealer` | 庄位 D 被遮挡 / 太暗 / 不在画面 |
| `stack:<seat>` | 摊牌手牌顶掉筹码胶囊、全下显示裸 `0`、弹窗遮挡 |
| `bet:<seat>` | 下注 pill 读成非金额文本 |
| `pot` / `blinds` | 底池横幅只显示标签、盲注行被融合读坏 |
| `vanished:<seat>` | 整座空白超过 `--max-vanish` 帧 |

消费端可以只读 `state` 记录；`discard` 不会写入输出。

---

## 7. 参数表

| 参数 | 默认 | 说明 |
|---|---|---|
| `--url` | `rtsp://127.0.0.1:8554/live` | MediaMTX RTSP 地址 |
| `--profile` | `config/table_wpk_1080x2400.json` | ROI 布局配置 |
| `--recognizer` | `roi` | `roi`=固定槽位；`full`=全图 OCR + 座位聚类 |
| `--full-config` | 无 | full 模式参数 JSON |
| `--initial-blinds` | 无 | 首帧盲注，如 `1/2`、`1/20`；之后正常继承 |
| `--lenient-chips` | 关 | 筹码单位读坏时取前导数字（固定筹码槽上下文） |
| `--every` | `0` | 采样间隔（秒）；`0` = 不节流，处理完当前帧立即取最新帧，中间帧丢弃 |
| `--settle` | `0.25` | 连续动作合并：动作区静止多少秒后才识别（`0` = 关闭，逐帧识别；仅 roi 路线） |
| `--settle-max` | `1.5` | 动作合并的最长等待秒数；动作一直不停时到点先识别一帧 |
| `--out` | `logs/live.states.jsonl` | JSONL 输出（每次启动覆盖） |
| `--stable-frames` | `1` | 新状态连续出现几帧才记录（`1` = 不做多帧确认） |
| `--max-vanish` | `3` | 整座空白连续几帧才认定离开 |
| `--name-confirm` | `3` | 名字连续确认几帧才更换 |
| `--dealer-carry` | `4` | full 模式 D 读不到时沿用上一庄位的最大帧数；`0` 关闭 |
| `--max-frame-age` | `2.0` | 超过多少秒的帧直接跳过 |
| `--crop` | `auto` | `auto`=裁非黑内容；`left`=按参考比例裁左侧；`none`=不裁 |
| `--no-dedup` | 关 | 关掉 ROI 去重 |
| `--no-fold` | 关 | 不读头像，省 9 块 ROI，失去弃牌检测 |
| `--dump-frames` | 无 | 采样帧存 JPEG |
| `--max-frames` | `0` | 最多处理多少帧（0 = 不限） |
| `--ai-advice` / `--no-ai-advice` | 开 | 轮到我时加载当前街技能并调用 AI 选一个选项（仅 roi 模式） |
| `--ai-timeout` | `20` | 单次 AI 建议超时（秒）；后台线程，不阻塞采样 |
| `--ai-confirm` | `1` | `hero_turn` 连续几帧才认定轮到我（蓝色下注按钮下第一帧即可） |
| `--quiet` | 关 | 只打状态行和最终汇总 |

---

## 8. 延迟与采样

| 指标 | 预期 |
|---|---|
| OBS 画面 → 服务端拿到帧 | 0.3–0.8s |
| 帧 → 写入 JSONL | p50 1.0–1.6s，p95 1.8–2.5s |
| 桌面变化 → 写入 JSONL（含防抖） | p50 2–3s，p95 3.5–4.5s |

单帧 OCR 现在通常 **0.2–0.5s**（取决于这一帧有多少 ROI 真的变了；名字/盲注/庄位
在一手内复用后，稳定帧只要 ~0.15s，换手/翻牌这类大变化会到 0.5–0.8s）。
worker 采用最新帧优先，处理不过来就丢中间帧，队列深度恒为 1。
开启动作合并时，单个动作会额外等 `--settle`（默认 0.25s）确认画面落定；连续动作
反而更快：中间帧全部跳过，只在最后识别一次，不会被一串半成品状态拖慢。

量延迟：

```bash
# 1) OBS 浏览器源加载 docs/live-clock.html，放在画面顶部
# 2) 启动 worker 并保存采样帧
.venv/bin/python -m poker_ocr.live --dump-frames /tmp/liveframes
# 3) 对比记录 ts、dump 帧文件名墙钟和画面里的时钟
```

---

## 9. 排障

| 现象 | 先查 |
|---|---|
| `[重连] 打不开 rtsp://...` | MediaMTX 是否启动；OBS 是否推流；路径是否叫 `live` |
| MediaMTX 有 publisher，worker 连不上 | RTSP 绑在 `127.0.0.1`，worker 必须在本机跑 |
| SRT 推不上 | 口令不一致；UDP 8890 被防火墙挡；`streamid=publish:live` 拼错 |
| 识别全错 / 大量丢弃 | 看 `stream_info.aspect` / `roi_fit.scale`；比例不对会自动换算，但 `scale < 0.5` 时分辨率已不够 |
| 庄位一直 `not_found` | 跑 `tools/validate_roi.py` 看叠加图；低分辨率会自动放大，仍失败可重标定 dealer 门槛 |
| 所有字段都空、ROI 复用率很高 | 画面根本不在槽位上；检查 `--crop`，或重新抓帧标定 |
| hero 筹码读不到 | 有/无手牌时筹码胶囊位置会移动；确认 profile 里有 `chip_slot_alt` |
| `proc_ms` 明显高于 600ms | CPU 争用；OBS 用 Apple VT 编码；试 `--no-fold` |
| `queue_ms` 持续变大 | 不应发生；检查是否有别的东西在消费邮箱 |
| 断流重连后第一条是重复基线 | 正常：重连会重置 ROI 缓存和状态机 |
| 盲注读不出（如 1/20） | 用 `--initial-blinds 1/20` 人工指定；OCR 自动读数仍按 2–10 比例闸过滤融合文本 |

---

## 10. 已知限制

- ROI profile（`table_wpk_1080x2400.json` / `table_wpk_live.json`）按 9 人桌标定；
  `Profile.load` 要求座位数与 `ring_order` 一致。
- 有效约 1.8Hz，不是严格 2Hz；忙时丢帧。
- 断流重连会把下一帧当新基线；若断在牌局中段，可能把全下误判为离座。
- 每次启动覆盖同一个 JSONL；只允许一个 worker 写同一个文件。
- 严格质量闸意味着摊牌、弹窗、动画帧会被丢弃，输出比实际帧稀疏。
- 不接后端/前端；当前只有文件和日志。
- **AI 建议只在 roi 路线可用**：全图路线没有每座的本街下注，也没有 `hero_turn`，
  算不出跟注额和选项金额。
- **「轮到我」默认看 hero 头像槽的蓝色下注按钮**（`thresholds.hero_turn_method
  = "blue_button"`），因此 `table_wpk_live.json` 没有 `hero_turn_probe` 也能用。
  只有当 profile 把 `hero_turn_method` 保持为 `preset_text` 时，才要求
  `hero_turn_probe` 槽位。
- 同一次采样里如果读到了两个人的动作，**先后顺序无法还原**，按座位环序记录；
  牌桌上的实际顺序可能不同。单次采样 0.5s，实际很少发生。
- 中途接管链路时（worker 在牌局中间启动），启动前的行动线缺失；翻前形态会用
  「最高下注 > 大盲」兜底判成至少一次加注，但更细的形态（3bet/4bet）判不出来。
- 模型延迟通常在数秒级，建议返回时 hero 可能已经行动；`advice` 记录照常落库，
  但实时提示价值随延迟下降。需要更快时换更快的模型/网关。

## 11. 测试

```bash
.venv/bin/python tests/run_all.py
```

共 16 个套件，覆盖位置推导、`.env` 加载、翻后规则、实时建议链路、庄位、
ROI 读取/去重、解析、状态机、全图路线、runtime profile、实时邮箱/限速/JSONL
和 HTTP 接口。
