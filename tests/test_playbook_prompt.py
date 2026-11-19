"""Harrington 书中计划段：渲染、零匹配与不伪造页码。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from types import SimpleNamespace

from poker_ocr.playbook import render_book_plan_text, render_book_plan_zh


def _sit(**overrides):
    base = dict(
        street="flop", pot_type="single-raised pot", opponents_in_hand=3,
        hand_tier="强成牌", has_draw=False, ground_truth=None, texture="干燥",
        spr=5.0, in_position=False,
        snap=SimpleNamespace(
            preflop_shapes=("single-raise",), to_call=0.0, hero_position="BB",
        ),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_multiway_strong_made_renders_book_plan():
    lines = render_book_plan_zh(_sit())
    text = "\n".join(lines)
    assert lines[0] == "## 书中计划（Harrington 现金桌）"
    assert "4+ 人" in text and "强成牌" in text
    assert "计划: 下注 0.67 池" in text
    assert "p301" in text
    assert "书中无此局面" not in text


def test_zero_match_says_no_page_without_faking_one():
    # 河牌在 #84 之后已被全量覆盖；用「未知牌力」的转牌点当零匹配样本。
    lines = render_book_plan_zh(_sit(street="turn", hand_tier="未知牌力"))
    text = "\n".join(lines)
    assert "书中无此局面" in text
    assert "p" not in text.replace("## 书中计划", "")


def test_text_is_short_enough_for_prompt_budget():
    text = render_book_plan_text(_sit())
    assert len(text) <= 800

if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
