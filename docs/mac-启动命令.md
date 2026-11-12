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
- **每次启动前先跑 [§1.6 固定窗口几何](#16-固定窗口几何每次启动前跑一次)**：窗口高度会飘（833 / 838 / 841 / 845 都见过），而点击是「归一化坐标 × 窗口矩形」——高度差几 pt，动作行就按比例偏几 pt。

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

### 1.5 四种窗口写法（`--url` 和 `--bet-slider-window` 通用）

| 写法 | 例子 | 说明 |
|---|---|---|
| `id:N` | `id:2354` | CoreGraphics 的 window_id，**标签页标题变化不影响**；窗口关闭/浏览器重启后会变 |
| `index:N` | `index:2` | 按枚举顺序取窗口，**被别的窗口盖住也能截到**；窗口增删后索引会变 |
| `title:...` | `title:WePoker-H5` | 按当前标签页标题匹配；切标签会失效 |
| `x,y,w,h` | `0,33,736,838` | 直接按屏幕矩形截（`screencapture -R`）；**被遮挡会截到上层窗口**，点击也要求窗口在最前 |

取到之后可以这样验证窗口尺寸是否和标定一致（启动日志会打印）：

```
[启动] 窗口源 title:WePoker-H5 → {'x': 0.0, 'y': 33.0, 'w': 736.0, 'h': 838.0}
[比例] 流 1472x1676（0.8783）→ 画面 1472x1676（0.8783）→ ROI 1472x1676（缩放 1.0000/1.0000）
```

如果 `[比例]` 不是 1472x1676 / 缩放 1.0000，说明窗口被拉过大小：ROI 是归一化的，还能跑，但内容可能整体偏几个像素。**每次启动前按 §1.6 把窗口钉成 736×838**（正好 = 1472×1676 @2x）。

### 1.6 固定窗口几何（每次启动前跑一次）

窗口高度会飘：磁贴只在拖到左边那一下算一次，之后就是普通窗口，浏览器重启还会沿用上次的 frame；换显示器 / 改缩放 / Dock 大小（这台是 tilesize=64、占 85pt）/ 菜单栏自动隐藏，也都会让可见区高度整体变几 pt。实测在这台机器上见过 **833 / 838 / 841 / 845** 四种高度。

而点击是「归一化坐标 × 窗口矩形」，**高度差 1pt，动作行（ny≈0.85）就偏约 0.85pt**。所以启动前显式钉一次，取 **736×838** —— `1472×1676 @2x` 正好等于 `config/table_wpk_mac.json` 的 `reference_size`，缩放 1.0000：

```bash
osascript -e 'tell application "Microsoft Edge" to set bounds of front window to {0, 33, 736, 871}'
```

> `set bounds` 的四个数是 **左, 上, 右, 下**，不是 `x,y,w,h`。最后一个 `871` 是**下边缘**：高 = 871 − 33 = **838**。
> 所以设完看到 `0,33,736,838` 是对的，不是“设了 871 却变成 838”。

宽度用 **736 而不是 735**（屏幕半宽）：1472 = 736×2，正好等于 profile 的参考宽度，Retina 下 1:1。

校对两条，都对才启动：

```bash
# ① 窗口：期望 0,33,736,838
nv tools/bet_slider.py --list-windows | grep -i edge

# ② 可见区：高度要和窗口 h 一致（这台是 1470x838，顶 33，底 85）
osascript -l JavaScript -e 'ObjC.import("AppKit"); const s=$.NSScreen.mainScreen, f=s.frame, v=s.visibleFrame; `可见区 ${v.size.width}x${v.size.height}，顶 ${f.size.height-(v.origin.y+v.size.height)}，底 ${v.origin.y}`'
```

- 两个高度不一致 ⇒ 窗口是浏览器恢复出来的旧 frame，重跑一次 `osascript` 再校对。
- 窗口定位后，Edge 的**标签页标题可能变成「新标签页」**，`title:WePoker-H5` 就找不到窗口：先点回 WePoker 标签，或 `--list-windows` 取 `index:N`。
- **OBS 画布也要是 1472×1676**（OBS → 设置 → 视频 → 基础/输出分辨率，源上右键 → 变换 → 适应屏幕）。画布停在 1472×1666 的话，流比窗口矮 5pt，点击还是会按比例偏。
- 验收：启动日志出现 `[比例] 流 1472x1676（0.8783）→ … 缩放 1.0000/1.0000` 且没有「宽高比偏差」警告，才说明**视频帧 / 窗口 / profile** 三套网格对齐了。

---

## 2. 启动命令

> 下面每条命令都假定已经跑过 §1.6（窗口 = `0,33,736,838`、OBS 画布 = 1472×1676）；没跑就先跑一次。

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

### 3.5 标注模式：把可点区域画到屏幕上

想确认「点击器认为的按钮位置」和屏幕上的真实按钮是否一致，用这个工具（不用开 live）：

```bash
nv tools/click_marks.py --window "title:WePoker-H5" --url "window:title:WePoker-H5"
```

- **蓝框 / 蓝圈** = 点击器的可点区域（profile 槽位 → 屏幕矩形）；**形状和帧里一致**：圆钮画成正圆（半径 = 短边/2，就是点击器的 disc 采样范围），滑杆/长条画方框；
- **绿十字** = OCR 按文字找到的按钮中心（和 `live._locate_click_targets` 同一套换算，所以这就是**真正会点到的位置**）；
- **红十字「出界 Npt」** = 文字中心跑到可点区域外 —— 点下去必偏，先查这个；
- 浮层透明、置顶、**点击穿透**，盖在游戏窗口上不影响操作；Ctrl-C 或 `--seconds 60` 结束。

`--url` 是**视频帧源**，要和 live 用同一个才有对照意义：

| 写法 | 看什么 |
|---|---|
| `--url cam:0` | 和 live 同源：帧几何 → 屏幕映射，**真实落点**（能看出那 10pt 偏下） |
| `--url "window:title:WePoker-H5"` | 帧=窗口截图：只验证 OCR 认得对不对（几何天然一致） |

和 live **同时跑**时摄像头可能被占，帧源改用 `window:title:...`（短写 `title:...` 会自动补 `window:`）。

启动会先打一段报告，重点看这两行：

```
视频帧          1472x1666 → 736x833.0pt   ROI 缩放 1.0000/1.0000
几何            帧比窗口矮 12.0pt：帧顶对齐时 动作行(ny≈0.85) 会偏低 ≈10.2pt
```

- `帧和窗口高度一致` → 几何没问题，只看十字有没有落在框里；
- 出现 `偏低/偏高` → 视频帧和窗口不是同一套网格（OBS 画布 / 窗口高度），先按 §1.6 对齐。

其他用法：

```bash
# 存一张带标注的真实截图（验证屏幕映射 / 发人看）
nv tools/click_marks.py --window "title:WePoker-H5" --url cam:0 \
    --save logs/marks.png --seconds 0

# 单帧离线核对（不连摄像头）：槽位和 OCR 命中点画在图上
nv tools/click_marks.py --window "title:WePoker-H5" \
    --image logs/mac_video/hero/002520.jpg --no-overlay --save logs/marks.png
```

> 圆钮画的是**正圆**（半径 = min(宽,高)/2），和点击器的 disc 采样完全一致；
> profile 里那个 52×52 的槽位只是搜索框，圆比框略小是正常的。

### 3.5.1 编辑模式：拖动改位置 / 改大小

框对不上就当场拖，不用手改 JSON：

```bash
# 先停掉 live（编辑器会挡住自动点击）！
nv tools/click_marks.py --window "title:WePoker-H5" --url "title:WePoker-H5" --edit
```

窗口铺在游戏位置，背景是**游戏画面截图**，13 个可点区域全在里面（含滑杆）：

| 操作 | 作用 |
|---|---|
| 拖框体 | 移动 |
| 拖四角 / 四边 | 改大小（8 个把手） |
| `Tab` / `Shift-Tab` | 换选中框 |
| `←→↑↓` / `Shift+方向` | 微调 1pt / 10pt |
| `R` | 重截背景 + OCR（按钮出现/消失后按一下） |
| `S` | 存回 profile 的 `global_slots`（先备份 `.json.bak`） |
| `Z` | 撤销上一笔 |
| `Esc` | 退出（有未保存改动要按两次） |

可编辑的 13 个区域：弃牌、免费/让牌、主钮(加注/下注)、跟注/让牌、5 个档位、
**滑杆轨道 / 滑杆金额 / 滑杆确定 / 滑杆关闭**。形状和真实按钮一致：圆钮画圆
（半径按短边算），滑杆轨道/金额气泡/关闭区画方框；选中圆钮时外面套一圈虚线
外接框 + 8 个把手，拖角改的还是外接框尺寸。

- 保存只改 `global_slots` 里对应那几行，其余键原样保留（单测覆盖了往返格式），
  **重启 live 生效**；
- 编辑器是普通置顶窗口（macOS 26 + Tk 9 去不掉标题栏，做不了透明浮层），
  但框的坐标就是点击网格，存出来的槽位是准的；
- 没有改的那几个框不会被改写；`S` 没改动时只提示、不动文件。

> 想校验改完的效果：关掉编辑器，再跑 §3.5 的标注模式（不带 `--edit`），
> 看蓝框是不是盖在真实按钮上。

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
| `osascript` 设了 871、窗口却是 838 高 | 正常：`set bounds` 是 **左/上/右/下**，871 是下边缘（871−33=838），不是高度 |
| `--list-windows` 的 Edge 高度不是 838（833/841/845） | 浏览器恢复的旧 frame，或换过显示器/Dock 大小：重跑 §1.6 的 `osascript` 再校对 |
| 启动日志比例不是 1472x1676 | 窗口被拉过大小：按 §1.6 设回 736×838；OBS 画布也改成 1472×1676 |
| 点击整体偏下 / 偏上 | 先比「视频帧 / 窗口 / 可见区」三个高度（§1.6）：帧比窗口矮（顶部对齐）会偏下，帧少了顶部 header 会偏上 |
| 想确认按钮识别 / 落点对不对 | 跑 §3.5 标注模式：蓝框=可点区域、绿十字=OCR 命中、「出界」=点下去必偏 |
| 按钮框位置/大小不对 | 跑 §3.5.1 编辑模式：拖框 / 拖角改，`S` 存回 profile（含滑杆四个区域） |
| `--auto-click 没有辅助功能权限` | 系统设置 → 隐私与安全性 → 辅助功能，勾终端后完全重启终端；先用 `--clicker-dry-run` |
| 点击没反应 | 游戏窗口不在最前（点击按屏幕坐标发）；截图不受遮挡但点击受 |
| `--bet-slider` 报缺窗口坐标 | 必须给 `--bet-slider-window`（或 `config/bet_slider_mac.json` 里填 `window`） |
| 滑杆打不开/金额不动 | 先跑 3.3 的手势验证；`drag_hold_ms` 80→120、`drag_max_px` 120→60 再试 |
| 高位金额到不了/来回抖 | 金额越大 px/BB 越小：把 `drag_fine_min_px` 2→1（客户端认 1px 时）、`drag_step_fraction` 1.0→0.5；确认网格不可达再开 `accept_ratio` 0.02–0.05（默认 0，绝不近似落注） |
| 看板端口被占 | `--web-port 8021`（只告警不阻塞识别） |
| 想重看某手 | Ctrl-C 结束；JSONL / 事件日志 / `data/hand_outbox` 都在 `logs/` 与 `data/` 下 |

---

## 6. 一条命令版（可直接粘）

> 先把 Edge 切到 WePoker 标签（`title:` 跟着当前标签走），再粘：

```bash
cd /Users/weiweigod/codes/poker-ocr && \
osascript -e 'tell application "Microsoft Edge" to set bounds of front window to {0, 33, 736, 871}' && \
nv tools/bet_slider.py --list-windows | grep -i edge && \
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
