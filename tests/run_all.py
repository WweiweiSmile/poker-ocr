#!/usr/bin/env python3
"""跑全部测试。

    .venv/bin/python tests/run_all.py

分层，从不需要图到需要图：
    test_positions  位置推导（纯逻辑，最快）
    test_env        .env 解析与加载（纯逻辑）
    test_coach      翻后建议引擎（纯逻辑，不需要图）
    test_ranges     翻前范围：169 键展开/宽度/具名与规则先验（纯逻辑）
    test_board_range 实时牌面范围：combo 分类/动作 mode/连接度（纯逻辑）
    test_field_ranges 快照 → 每对手范围 ID + mode（纯逻辑）
    test_hand_vs_ranges 手牌 vs 范围：精确 behind + MC 权益（纯逻辑）
    test_hand_strength 实时牌力解析：behind/equity 合成一行结论（纯逻辑）
    test_advice     实时 AI 建议：行动线/中文事实/选项/模型协议（纯逻辑）
    test_gto_lookup GTO 近似表查询：同构映射/节点/插值/保守 None（纯逻辑）
    test_hand_id     手牌 ID：名单/位置/日期摘要 + 碰撞后缀（纯逻辑）
    test_hand_outbox 手牌 outbox：一手一文件/幂等/sent 迁移（纯逻辑）
    test_opponent_profile 对手画像：弃牌补全/统计/分类/pin/建议集成（纯逻辑）
    test_clicker    桌面自动点击：坐标/配置/档位映射/安全闸（纯逻辑，不点鼠标）
    test_ai_advice_replay 复盘手牌 → 决策点快照的重建（纯逻辑，不连库不调模型）
    test_dealer     庄位检测（合成图，不依赖照片）
    test_roi_reader ROI 读取器（合成图 + 假 OCR 引擎，不依赖照片和模型）
    test_roi_dedup  ROI 去重（合成图，不依赖照片和模型）
    test_parse      字段解析（真实 OCR 原文变体）
    test_frame_state 桌面状态 + 变化指纹（手工拼 RoiFrame）
    test_state_stream 防抖/丢弃/消失确认/断流重置（桩 RoiStream）
    test_live       实时邮箱/限速/重连/JSONL（不连 RTSP）
    test_full_table 全图 OCR/座位聚类/D 庄位/位置（需要 table-8p.jpg）
    test_runtime_profile 全图标定 → runtime profile → ROI 回读（需要 table-8p.jpg）
    test_stable_cards 跨帧牌面稳定闸（纯逻辑，不读图）
    test_doubt_window 疑惑帧时间窗仲裁（纯逻辑，不读图）
    test_export_live_hands 从 live.states.jsonl 导出完整手牌（纯逻辑）
    test_cards      公共牌 / hero 手牌识别（需要 images/card 标注样本）
    test_card_replay 真实样张回放：一手牌只落一条记录、牌面只前进
    test_extract    端到端提取（需要 table.jpg）
    test_api        HTTP 接口（需要 table.jpg + fastapi）
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

SUITES = ["test_positions.py", "test_env.py", "test_coach.py",
          "test_ranges.py", "test_board_range.py", "test_field_ranges.py",
          "test_hand_vs_ranges.py", "test_hand_strength.py", "test_advice.py",
          "test_gto_lookup.py",
          "test_hand_id.py", "test_hand_outbox.py",
          "test_opponent_profile.py", "test_clicker.py",
          "test_ai_advice_replay.py", "test_dealer.py", "test_roi_reader.py",
          "test_roi_dedup.py", "test_parse.py", "test_frame_state.py",
          "test_state_stream.py", "test_live.py", "test_full_table.py",
          "test_runtime_profile.py", "test_stable_cards.py",
          "test_doubt_window.py", "test_export_live_hands.py",
          "test_beat_rate.py",
          "test_ground_truth.py",
          "test_build_sft_dataset.py",
          "test_preflop_ranges.py", "test_cards.py",
          "test_card_replay.py", "test_extract.py", "test_api.py"]


def main() -> int:
    failed = []
    for name in SUITES:
        print(f"── {name} " + "─" * max(0, 50 - len(name)), flush=True)
        proc = subprocess.run([sys.executable, str(HERE / name)])
        if proc.returncode != 0:
            failed.append(name)

    print()
    if failed:
        print(f"❌ 失败的套件：{', '.join(failed)}")
        return 1
    print(f"✅ {len(SUITES)} 个套件全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
