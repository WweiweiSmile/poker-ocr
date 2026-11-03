# poker-ocr 项目全景爆炸图

用 Archify（本机 `~/.agents/skills/archify`）生成的单文件交互式 HTML：把「实时识别 → AI 决策 → 手牌沉淀 → 复盘分析」拆成两条带，展示数据流转与业务关联。

- 打开：`docs/archify/poker-ocr-overview.html`（双击即可，无需起服务）
- 规格：`docs/archify/poker-ocr-overview.json`（改图只改这个文件）

## 图怎么读

**上带 · 实时链路（每帧 ~0.5s）**

```
牌桌 App 画面 → ROI 识别 → 帧状态机 → 手牌追踪 → AI 建议 ⇄ LLM API
                                                        │
                                                        └─ 自动点击（虚线回到牌桌）
```

- 左端是外部输入（手机 → OBS 虚拟摄像头 / RTSP），右端是外部 LLM，二者都在系统边界外。
- 橙色虚线框是 `live.py` 本机进程；虚线紫色回路表示建议落成真实点击。

**下带 · 数据沉淀与复盘（每手 / 离线）**

```
帧状态机 ──状态/手牌 JSONL──▶ live.states.jsonl ──▶ 离线分析 tools/
手牌追踪 ──手牌记录────────▶ 手牌 outbox ──▶ 手牌库 replay-console ──▶ 复盘控制台
```

- 两条本地事实源：`live.states.jsonl`（只追加）与 `data/hand_outbox/`（一手一文件）。
- live 进程不直接写 MySQL；outbox 失败留在 `pending/`，重试幂等。
- 带 sources 的节点在查看器里提供源码入口（仓库路径 + 行号，固定在生成时的 commit）。

## 4 个引导视图

页面顶部的「引导视图」按链路聚焦，不用在整张图上找：

1. **实时链路**：采集 → 识别 → 状态 → 手牌 → 建议
2. **决策闭环**：建议如何回到牌桌（自动点击）
3. **手牌沉淀**：JSONL / outbox / 手牌库
4. **复盘与分析**：手牌库、控制台、离线报表

## 改图与重新生成

```bash
# 只校验（改完先跑这个；showcase 要求 9/9 且 0 error / 0 warning）
node ~/.agents/skills/archify/bin/archify.mjs validate architecture \
  docs/archify/poker-ocr-overview.json --quality showcase --repo-root . --json

# 交付 HTML（--repo-root 用于校验 sources 里的源码行）
node ~/.agents/skills/archify/bin/archify.mjs deliver architecture \
  docs/archify/poker-ocr-overview.json docs/archify/poker-ocr-overview.html \
  --quality showcase --repo-root . --json

# 可选：浏览器证据（截图 + 四档视口收纳检查）
ARCHIFY_CHROME="/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" \
  node ~/.agents/skills/archify/bin/archify.mjs visual-check \
  docs/archify/poker-ocr-overview.html --json
```

注意：

- `meta.repository.revision` 固定的是生成时的 commit；换 commit 后源码链接会指旧版本，需要同步更新。
- 新增节点时优先加在对应带里，保持「一条主路径 + 少量分支」；超过 12 个主节点会明显变挤。
- 交付后不要手改 HTML；改 JSON 重新 `deliver`，否则 sha256 对不上。
