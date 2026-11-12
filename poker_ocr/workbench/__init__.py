"""工作台（Workbench）：本地 Web 工作区，管理 live 子进程。

    nv -m poker_ocr.workbench            # http://127.0.0.1:8030

设计口径见 `docs/工作台-需求与设计-2026-11-12.md`：

  · **只引用，不复制**：工作区只存「运行配方」，识别几何仍在
    `config/table_*.json`，标定直接改那个文件（备份 + 原子写 + diff）。
  · **一次一路**：同一时刻只跑一个 live；数据模型保留 `run` 维度。
  · **不启动外部程序**：只检测/消费 OBS、MediaMTX、浏览器、摄像头。
  · **live 只增只读/控制端点**：预览、元数据、点击开关；看板 UI 仍只读。
  · **fail open**：浮层、体检、预览任何一步失败都不能影响识别主链路。

包结构：

    store.py        工作区 CRUD、profile 发现、schema 校验
    geometry.py     profile 槽位 → 分组 / 像素 / 几何诊断
    sources.py      窗口/摄像头/RTSP/文件 的枚举与单帧探测
    preflight.py    启动前体检（17 项，id/status/title/detail/fix）
    supervisor.py   live 子进程：spawn / 停止 / 重启 / manifest
    live_client.py  访问 live 的 /api/meta、/api/preview.jpg、控制接口
    calibration.py  草稿、试读 OCR、diff、备份、原子写
    overlay_ctl.py  屏幕浮层（复用 overlay.py，Pillow 渲染）
    service.py      编排层（app.py 只做 HTTP 薄壳，测试直接测这里）
    app.py          FastAPI 路由 + 静态页
"""

from __future__ import annotations

__all__ = ["SCHEMA_VERSION"]

SCHEMA_VERSION = 1
