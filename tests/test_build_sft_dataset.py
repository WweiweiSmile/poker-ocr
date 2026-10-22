#!/usr/bin/env python3
"""SFT 数据集导出的测试：标签合法性、陈旧帧过滤、按手牌切分。

    .venv/bin/python tests/test_build_sft_dataset.py

全部用合成日志，不读真实 hands.json、不连模型。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.build_sft_dataset import build  # noqa: E402


def state(ts: str, hand_id: str, *, pot: float, hero_bet: float,
          opp_bet: float, position: str = "BB", board=(),
          cards=("Ah", "Kd")) -> dict:
    return {
        "type": "state", "ts": ts, "hand_id": hand_id,
        "hero": {"seat_id": "hero", "stack": 100.0, "bet": hero_bet,
                 "fold": False},
        "opponents": [{"seat_id": "o1", "position": "CO", "stack": 100.0,
                       "bet": opp_bet, "fold": False}],
        "pot": pot, "pot_before_street": pot - hero_bet - opp_bet,
        "hero_cards": list(cards), "board": list(board),
        "hero_position": position, "table_seats": 9,
        "players_in_hand": 2,
        "blinds": {"sb": 0.5, "bb": 1.0, "ante": 0.5, "raw": "1/2(1)"},
    }


def advice(ts: str, hand_id: str, option: str, *, pot: float,
           to_call: float, street: str = "preflop", board=(),
           cards=("Ah", "Kd")) -> dict:
    return {
        "type": "advice", "ts": ts, "hand_id": hand_id, "street": street,
        "hero_cards": list(cards), "board": list(board),
        "pot": pot, "to_call": to_call,
        "option": {"id": option, "label": option, "kind": "fold"},
        "reason": f"because {option}",
        "model": "stub", "source": "model",
    }


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "live.states.jsonl"
        hands = Path(tmp) / "hands.json"
        # h1：BB 面对 3BB 开池 → fold（合法）
        # h2：同一手，第二次建议用了一个已经不存在的 id → 跳过
        # h3：BTN 翻前 to_call=0 但底池已经很大 → 陈旧帧，跳过
        # h4：BB 翻牌面对下注 → call（合法）
        rows = [
            state("2026-01-01T00:00:01", "h1", pot=4.5, hero_bet=0.0,
                  opp_bet=3.0),
            advice("2026-01-01T00:00:02", "h1", "fold", pot=4.5, to_call=3.0),
            state("2026-01-01T00:00:03", "h2", pot=4.5, hero_bet=0.0,
                  opp_bet=3.0),
            advice("2026-01-01T00:00:04", "h2", "open-9", pot=4.5, to_call=3.0),
            state("2026-01-01T00:00:05", "h3", pot=20.0, hero_bet=6.0,
                  opp_bet=6.0, position="BTN"),
            advice("2026-01-01T00:00:06", "h3", "call", pot=20.0, to_call=0.0),
            state("2026-01-01T00:00:07", "h4", pot=30.0, hero_bet=0.0,
                  opp_bet=10.0, board=("Ah", "Kd", "7c")),
            advice("2026-01-01T00:00:08", "h4", "call", pot=30.0, to_call=10.0,
                   street="flop", board=("Ah", "Kd", "7c")),
        ]
        log.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                       encoding="utf-8")
        hands.write_text("[]", encoding="utf-8")

        got, train, evals, stats = build(
            log, hands, "lean", with_reason=False, eval_frac=0.5, seed=1)

    assert len(got) == 2, f"应保留 2 条，实际 {len(got)}"
    assert stats["stale"] == 1, f"陈旧帧应跳过 1 条：{dict(stats)}"
    assert stats["label-not-in-options"] == 1, f"非法 id 应跳过 1 条：{dict(stats)}"
    assert len(train) + len(evals) == 2
    # 按手牌切分：一只手不会同时出现在 train 和 eval
    assert len(evals) == 1 and len(train) == 1, (len(train), len(evals))

    row = got[0]["row"]
    assert set(row) == {"instruction", "input", "output", "system"}, row.keys()
    assert json.loads(row["output"])["option"] in got[0]["meta"]["legal"]
    assert "Options" in row["instruction"] or "option" in row["instruction"].lower()
    assert len(row["system"]) > 1000

    print("OK  2 条保留 / 1 条陈旧 / 1 条非法 id / 按手牌切分 1:1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
