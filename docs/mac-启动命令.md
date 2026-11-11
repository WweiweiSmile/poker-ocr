# macOS 启动命令（WePoker H5 / `table_wpk_mac`）

实时链路：截 **游戏浏览器窗口** → 固定 ROI 识别 → 轮到我时调 AI → 自动点击 / 长按滑杆下注 → Web 看板。
本文只放**复制即用**的命令；参数语义和内部链路见 [live-stream.md](live-stream.md)。

---

## 0. 前置检查

```bash
cd /Users/weiweigod/codes/poker-ocr
ls nv config/table_wpk_mac.json config/bet_slider_mac.json .env
```

- `.env` 里要有 `AI_SERVER_API_KEY` / `AI_SERVER_BASE_URL` / `AI_SERVER_MODEL`（启动日志会打印「凭据来源 dotenv」）。
- **自动点击需要辅助功能权限**：系统设置 → 隐私与安全性 → 辅助功能，勾上运行 Python 的终端（Terminal / iTerm / VS Code），改完完全退出并重开终端。
- **只识别不点击**时不需要这个权限（加 `--clicker-dry-run` 或 `--no-auto-click`）。

---

## 1. 取游戏窗口的 xywh

窗口坐标是**逻辑点**（Retina 2x 下截出来是 2 倍像素：736×838 点 → 1472×1676 像素，正好等于 `table_wpk_mac.json` 的 `reference_size`）。

### 1.1 列出所有窗口（推荐）

```bash
nv tools/bet_slider.py --list-windows
```

输出示例（`索引 | owner | title | x,y,w,h`）：

```
[0] id=772   DeepSeek Harness | 重定位 hero 视频 ROI — DeepSeek Harness | 0,33,1470,833
[2] id=2354  Microsoft Edge   | WePoker-H5                            | 0,33,736,838
[4] id=2753  OBS Studio       | OBS 33.0.1 - …                        | 1,34,1468,831
```

> **title 是当前标签页的标题**。录屏/看板把 Edge 切到别的标签时，`WePoker-H5` 会变；
> 这时先点回 WePoker 标签，或者改用 `index:N` / `x,y,w,h`。

### 1.2 只打印可复制的 xywh

```bash
nv - <<'PY'
from poker_ocr.bet_slider import list_windows
for w in list_windows():
    print(f"index:{w.index}  {w.owner} | {w.title} | "
          f"{w.rect.x:g},{w.rect.y:g},{w.rect.w:g},{w.rect.h:g}  id={w.window_id}")
PY
```

### 1.3 只找 WePoker（标题或进程名）

```bash
nv - <<'PY'
from poker_ocr.bet_slider import list_windows
for w in list_windows():
    if "WePoker" in w.title or "WePoker" in w.owner:
        print(f"index:{w.index}  x,y,w,h={w.rect.x:g},{w.rect.y:g},{w.rect.w:g},{w.rect.h:g}  id={w.window_id}")
PY
```

### 1.4 机器可读（脚本里用）

```bash
nv tools/bet_slider.py --list-windows --json
```

### 1.5 三种窗口写法（`--url` 和 `--bet-slider-window` 通用）

| 写法 | 例子 | 说明 |
|---|---|---|
| `index:N` | `index:2` | 用 CoreGraphics 的窗口 id，**被别的窗口盖住也能截到**；窗口增删后索引会变 |
| `title:...` | `title:WePoker-H5` | 按当前标签页标题匹配；切标签会失效 |
| `x,y,w,h` | `0,33,736,838` | 直接按屏幕矩形截（`screencapture -R`）；**被遮挡会截到上层窗口**，点击也要求窗口在最前 |

取到之后可以这样验证窗口尺寸是否和标定一致（启动日志会打印）：

```
[启动] 窗口源 title:WePoker-H5 → {'x': 0.0, 'y': 33.0, 'w': 736.0, 'h': 838.0}
[比例] 流 1472x1676（0.8783）→ 画面 1472x1676（0.8783）→ ROI 1472x1676（缩放 1.0000/1.0000）
```

如果 `[比例]` 不是 1472x1676 / 缩放 1.0000，说明窗口被拉过大小：ROI 是归一化的，还能跑，但内容可能整体偏几个像素。需要严格对齐时把窗口设成 736×838（Edge）：

```bash
osascript -e 'tell application "Microsoft Edge" to set bounds of front window to {0, 33, 736, 871}'
# bounds = 左, 上, 右, 下 → 736x838
```

---

## 2. 启动命令

### 2.1 首次运行：只识别 + AI + 看板，不真的点鼠标（推荐）

```bash
cd /Users/weiweigod/codes/poker-ocr

nv -m poker_ocr.live \
  --profile config/table_wpk_mac.json \
  --url "window:title:WePoker-H5" \
  --initial-blinds 1/2 \
  --bet-slider \
  --bet-slider-window "title:WePoker-H5" \
  --bet-slider-config config/bet_slider_mac.json \
  --auto-click --clicker-dry-run \
  --web-open \
  --out logs/live.states.jsonl
```

- 识别 / 建议照常跑，点击只打印计划（dry-run），不需要辅助功能权限；
- 看板：`http://127.0.0.1:8020`。

### 2.2 正式运行：档位优先，滑杆兜底

```bash
cd /Users/weiweigod/codes/poker-ocr

nv -m poker_ocr.live \
  --profile config/table_wpk_mac.json \
  --url "window:title:WePoker-H5" \
  --initial-blinds 1/2 \
  --bet-slider \
  --bet-slider-window "title:WePoker-H5" \
  --bet-slider-config config/bet_slider_mac.json \
  --auto-click \
  --web-open \
  --out logs/live.states.jsonl
```

默认每次点击前终端问 `y/n`；确认稳定后加 `--no-confirm` 全自动（谨慎）。

### 2.3 只走滑杆：完全不用快捷档位

```bash
cd /Users/weiweigod/codes/poker-ocr

nv -m poker_ocr.live \
  --profile config/table_wpk_mac.json \
  --url "window:title:WePoker-H5" \
  --initial-blinds 1/2 \
  --bet-slider \
  --bet-slider-only \
  --bet-slider-window "title:WePoker-H5" \
  --bet-slider-config config/bet_slider_mac.json \
  --auto-click \
  --web-open \
  --out logs/live.states.jsonl
```

- `--bet-slider-only`：`bet`/`raise` 无条件走「长按 → 拖手柄 → 确定」，跳过 5% 档位匹配、置灰检测和档位截图；
- `call`/`fold`/`check` 仍走各自按钮；主动全下也走滑杆（拖/滚到顶部气泡出现
  **ALLIN/全下** 字样才确定，连续两次读到才算）。

### 2.4 只识别，不调 AI、不点击（排查 ROI / 牌面用）

```bash
nv -m poker_ocr.live \
  --profile config/table_wpk_mac.json \
  --url "window:title:WePoker-H5" \
  --initial-blinds 1/2 \
  --no-ai-advice --no-auto-click \
  --web-open \
  --out logs/live.states.jsonl
```

### 2.5 用显式 xywh 而不是标题

```bash
nv -m poker_ocr.live \
  --profile config/table_wpk_mac.json \
  --url "window:0,33,736,838" \
  --initial-blinds 1/2 \
  --bet-slider --bet-slider-window "0,33,736,838" \
  --bet-slider-config config/bet_slider_mac.json \
  --auto-click --clicker-dry-run \
  --web-open --out logs/live.states.jsonl
```

> 用 `x,y,w,h` 时截图不带窗口 id，**游戏窗口不能被盖住**；用 `index:` / `title:` 时截图不受遮挡，但**点击仍要求窗口在最前**（鼠标事件按屏幕坐标发送）。

### 2.6 测试模式（演练，不写手牌库）

在任意上面的命令里加 `--test`：默认写 `logs/live.test.jsonl`（可用 `--out` 覆盖），手牌只进日志、不写 `data/hand_outbox`。

---

## 3. 启动后怎么验证

### 3.1 识别是否正常

看启动日志的 `[比例] 流 …` 是否为 1472x1676；再看几行 `✅ 记录` 的 `pot / 轮到我 / 对手` 是否在更新。

### 3.2 滑杆识别（零副作用）

```bash
nv tools/bet_slider.py \
  --image logs/incidents/<某张滑杆截图>.jpg \
  --target 5.5 --dry-run
```

期望 `滑杆 opened=True amount=…BB`。

### 3.3 滑杆手势闭环，但不落注

**必须轮到你、画面出现「下注/加注」按钮时跑**（否则长按点可能落到别的按钮上）：

```bash
nv tools/bet_slider.py --apply --target 999 \
  --window "title:WePoker-H5" \
  --profile config/table_wpk_mac.json \
  --config config/bet_slider_mac.json --json
```

给一个到不了的目标：它会打开滑杆、拖到顶（顶部出现 ALLIN），报
`target_exceeds_allin`、点右侧空白关掉，**不会点确定**。期望 `drags > 0`、
`evidence.cancel_closed = true`。

### 3.4 实战日志要看到什么

```bash
grep -E "\[滑杆\] 拖动|bet_slider|街已推进|重复" logs/live.log | tail -20
```

- 正常：`[滑杆] 拖动 +27px → 5.5BB（目标 5.5）` → `[点击] 已点击 bet [bet_slider]：目标 5.5BB，实际 5.5BB，拖动 3 次`；
- 不应该出现同一回合连问两次；旧街建议晚到只会是 `[点击] 跳过：街已推进（旧街建议作废）`。

---

## 4. 常用参数速查

| 参数 | 默认 | 作用 |
|---|---|---|
| `--profile` | `table_wpk_1080x2400` | 用 `config/table_wpk_mac.json`（mac 窗口几何） |
| `--url` | `cam:auto` | `window:title:WePoker-H5` / `window:index:N` / `window:x,y,w,h` |
| `--initial-blinds` | 无 | 首帧盲注兜底，单挑桌建议固定 `1/2`（盲注行会粘房间号，解析不了） |
| `--bet-slider` | 关 | 下注/加注走长按滑杆（需 `--auto-click` + 窗口坐标） |
| `--bet-slider-only` | 关 | 只走滑杆，完全不碰快捷档位（需 `--bet-slider`） |
| `--bet-slider-window` | 无 | 滑杆窗口，写法同 `--url`；**开了 `--bet-slider` 必填** |
| `--bet-slider-config` | `config/bet_slider_mac.json` | 长按/拖动/取整/取消参数 |
| `--auto-click` | 开 | 建议返回后自动点击 |
| `--clicker-dry-run` | 关 | 只打印点击计划，不发真实事件 |
| `--no-confirm` | 关 | 点击前不再问 y/n |
| `--ai-advice` | 开 | 关掉它（`--no-ai-advice`）会自动关闭 auto-click |
| `--web` / `--web-open` | 开 / 关 | 看板 `127.0.0.1:8020`，`--web-open` 启动后自动开浏览器 |
| `--out` | `logs/live.states.jsonl` | JSONL 输出；事件/控制台日志按同名派生 |
| `--test` | 关 | 演练模式：独立日志、手牌不进手牌库 |
| `--roi-threads` | 4 | 帧内 ROI 并行线程数 |
| `--quiet` | 关 | 只打状态行和汇总 |

---

## 5. 常见问题

| 现象 | 处理 |
|---|---|
| `没找到窗口 'WePoker-H5'` | 标签页不在 WePoker（title 跟着当前标签走）：点回那个标签，或 `--list-windows` 拿 `index:N` |
| 启动日志比例不是 1472x1676 | 窗口被拉过大小：`osascript` 设回 736×838，或接受几像素偏差 |
| `--auto-click 没有辅助功能权限` | 系统设置 → 隐私与安全性 → 辅助功能，勾终端后完全重启终端；先用 `--clicker-dry-run` |
| 点击没反应 | 游戏窗口不在最前（点击按屏幕坐标发）；截图不受遮挡但点击受 |
| `--bet-slider` 报缺窗口坐标 | 必须给 `--bet-slider-window`（或 `config/bet_slider_mac.json` 里填 `window`） |
| 滑杆打不开/金额不动 | 先跑 3.3 的手势验证；`drag_hold_ms` 80→120、`drag_max_px` 120→60 再试 |
| 看板端口被占 | `--web-port 8021`（只告警不阻塞识别） |
| 想重看某手 | Ctrl-C 结束；JSONL / 事件日志 / `data/hand_outbox` 都在 `logs/` 与 `data/` 下 |

---

## 6. 一条命令版（可直接粘）

```bash
cd /Users/weiweigod/codes/poker-ocr && \
nv -m poker_ocr.live \
  --profile config/table_wpk_mac.json \
  --url "window:title:WePoker-H5" \
  --initial-blinds 1/2 \
  --bet-slider --bet-slider-only \
  --bet-slider-window "title:WePoker-H5" \
  --bet-slider-config config/bet_slider_mac.json \
  --auto-click --web-open \
  --out logs/live.states.jsonl
```
