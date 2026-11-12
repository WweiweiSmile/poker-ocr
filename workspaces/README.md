# 工作区（Workspace）

一个文件 = 一次启动所需的全部配方（schema 1）：

- `profile`：引用 `config/table_*.json`（**不复制几何**）；
- `source`：live 的 `--url` 写法（`window:title:...` / `cam:auto` / `rtsp://...` / 视频文件）；
- `window`：期望尺寸/位置（逻辑点，位置默认 `[0,0]`）与手动固定记录；
- `click` / `runtime` / `debug`：点击策略、运行参数、预览与浮层开关。
  `window.spec` 是**滑杆点击/浮层要落到的游戏窗口**：窗口源留空 = 跟随源；
  摄像头/RTSP/文件源必须显式填（识别走 OBS 不代表鼠标能点到 OBS 画面）。
  从窗口源切到摄像头后，工作台会把原窗口自动写进 `window.spec`（保存时迁移）。
  写法推荐 `id:N`（CoreGraphics window_id，标签页标题变了也不影响；窗口关闭或
  浏览器重启后需要重选），也支持 `index:N` / `title:...` / `owner:...`。
  `window.position` 是一键配置时把窗口移动到的左上角（默认 `[0,0]`，多屏可为负）。

仓库自带一个可提交的示例 `example-wpk-mac.json`（窗口源 + 736×838 + dry-run）；
本机实际使用的工作区（如 `wpk-mac-live.json`，含窗口 id / 摄像头索引）属于本地配置，
不要直接提交给别的机器用。

## 怎么来

由工作台创建（页面「+ 从 profile 新建」/「复制」/「导入 JSON」）：

```bash
./nv -m poker_ocr.workbench
# http://127.0.0.1:8030
```

也可以手写 JSON 后导入；工作台会按 schema 校验（字段路径 + 原因）。

## 约定

- 文件名就是 `id`：`<id>.json`，只能用小写字母/数字/`._-`，最长 64。
- 工作区**只引用** profile。标定会直接改 `config/table_*.json`（自动备份
  `config/*.bak-*`，保留最近 20 份），影响所有引用同一 profile 的命令。
- 标定页默认按宽度自适应；空白处拖框可直接重画选中槽位，检查器可输入归一化坐标，
  方向键按「步长」微调，适合快速修正漂移的槽位。
- 工作台「窗口 → 一键配置窗口分辨率」会把游戏窗口调整到 `window.expected_size`
  （显式点击，需要辅助功能权限；系统不满足时会记录实际尺寸并提示）。
- 建议提交到仓库（不含密钥）；`runs/` 是本机运行产物，已在 `.gitignore` 里。
- `expected_size` 是**窗口逻辑点**，不是 profile 的 `reference_size`（像素）：
  Retina 2x 下两者差一倍。拿不准就留空，打开游戏窗口后点「固定窗口」。

完整字段说明与实现细节见 [../docs/工作台-实现说明.md](../docs/工作台-实现说明.md)。
